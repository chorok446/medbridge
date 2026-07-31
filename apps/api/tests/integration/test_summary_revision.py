"""요약 무효화 — OCR 완료 시 revision 증가·청크 자동 재생성·요약 stale, 반복 문구 출처."""

import uuid

from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobType
from app.models.search import DocumentChunk
from app.services.extraction.ocr import OcrResult
from app.services.ocr import service as ocr_service
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs
from tests.integration.test_ocr_flow import fake_words
from tests.integration.test_summary_api import enable_deterministic, make_summary, upload_chunked


class TestOcrInvalidatesSummary:
    async def test_ocr_completion_bumps_revision_rebuilds_chunks_and_stales_summary(
        self, client, monkeypatch
    ):
        await enable_deterministic()
        fake = type(
            "FakeEngine",
            (),
            {
                "available": True,
                "version": "fake-1.0",
                "recognize_page": lambda self, pdf_path, page_number, **kw: OcrResult(
                    page_number=page_number,
                    words=fake_words(["OCR", "인식", "문장", "예시"]),
                ),
            },
        )()
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)

        doc_id = await upload_chunked(client, fx.mixed_digital_and_scanned())
        duuid = uuid.UUID(doc_id)
        await make_summary(client, doc_id)

        async with get_session_factory()() as s:
            doc = await s.get(Document, duuid)
            rev_before = doc.content_revision
        status = (
            await client.get(f"/api/documents/{doc_id}/summaries/status")
        ).json()["data"]
        assert status["stale"] is False

        # OCR 실행 → 완료 시 revision++ + 청크 자동 재생성
        res = await client.post(f"/api/documents/{doc_id}/ocr")
        assert res.status_code == 200
        await drain_jobs()

        async with get_session_factory()() as s:
            doc = await s.get(Document, duuid)
            assert doc.content_revision > rev_before  # revision 증가
            # 청크 자동 재생성 잡이 등록·실행됨
            chunk_jobs = (
                await s.execute(
                    select(func.count()).select_from(DocumentJob).where(
                        DocumentJob.document_id == duuid,
                        DocumentJob.job_type == JobType.CHUNK_REBUILD,
                    )
                )
            ).scalar_one()
            assert chunk_jobs >= 1
            # 재생성이 끝났으면 chunk_revision이 content_revision을 따라잡는다
            assert doc.chunk_revision == doc.content_revision

        # 기존 요약은 stale로 표시된다 (최신처럼 보이지 않는다)
        status = (
            await client.get(f"/api/documents/{doc_id}/summaries/status")
        ).json()["data"]
        assert status["stale"] is True

    async def test_summary_blocked_when_chunks_not_yet_updated(self, client):
        """content_revision이 올랐지만 청크가 아직 갱신 전이면 요약 생성을 막는다."""
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        duuid = uuid.UUID(doc_id)
        async with get_session_factory()() as s:
            doc = await s.get(Document, duuid)
            doc.content_revision = doc.content_revision + 1  # 청크보다 앞선 상태로 강제
            await s.commit()
        r = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert r.status_code == 409


class TestRepeatedPhraseSource:
    async def test_artifact_preserves_all_real_sources_across_pages(self, client):
        """같은 문구가 여러 페이지에 있어 청크가 중복 제거돼도, 요약 artifact는 그 청크의
        모든 실제 출처(page 여러 개)를 보존한다."""
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        duuid = uuid.UUID(doc_id)

        # 청크 중복 제거 후에도 페이지 1·2 출처가 함께 남아 있어야 한다
        async with get_session_factory()() as s:
            chunks = (
                await s.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == duuid)
                )
            ).scalars().all()
        pages_in_refs = {
            ref["pageNumber"] for c in chunks for ref in c.source_refs_json
        }
        assert {1, 2} <= pages_in_refs

        await make_summary(client, doc_id)
        listed = (await client.get(f"/api/documents/{doc_id}/summaries")).json()["data"]
        # 최소 한 artifact가 여러 페이지 출처를 보존한다
        multi_page = [
            a
            for a in listed["artifacts"]
            if len({r["pageNumber"] for r in a["sourceRefs"]}) >= 2
        ]
        assert multi_page
