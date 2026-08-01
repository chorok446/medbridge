"""추출 통합 테스트 — 업로드→추출→조회→재처리→취소→삭제 전 구간."""

import asyncio
import threading
import uuid

from sqlalchemy import func, select, text

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStatus
from app.models.extraction import DocumentBlock, DocumentPage, DocumentWord
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


def upload_kwargs(data: bytes, filename: str = "fixture.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


class TestExtractionFlow:
    async def test_two_column_document_end_to_end(self, client):
        doc = await upload_extracted(client, fx.two_column_english(pages=3))
        assert doc["processingStatus"] == "extracted"

        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        assert len(pages) == 3
        for p in pages:
            assert p["extractionStatus"] == "extracted"
            assert p["scanVerdict"] == "digital"
            assert p["width"] > 0 and p["height"] > 0

        # 페이지 상세: 좌→우 읽기 순서가 정규화 본문에 반영
        detail = (await client.get(f"/api/documents/{doc['id']}/pages/1")).json()["data"]
        text = detail["normalizedText"]
        assert text.index("L0-0") < text.index("R0-0")
        # 반복 머리말은 본문에서 제외 (블록으로는 존재)
        assert "Journal of Cardiology Study" not in text

        blocks = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/blocks?include_bands=true")
        ).json()["data"]
        assert any(b["isHeader"] for b in blocks)
        assert any(b["isFooter"] for b in blocks)
        for b in blocks:
            assert b["x0"] <= b["x1"] and b["y0"] <= b["y1"]
            assert 0 <= b["x0"] and b["x1"] <= detail["width"]
            assert 0 <= b["y0"] and b["y1"] <= detail["height"]

        no_bands = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/blocks?include_bands=false")
        ).json()["data"]
        assert all(not (b["isHeader"] or b["isFooter"]) for b in no_bands)

    async def test_rotated_page_stored(self, client):
        doc = await upload_extracted(client, fx.rotated_page())
        page = (await client.get(f"/api/documents/{doc['id']}/pages/1")).json()["data"]
        assert page["rotation"] == 90
        blocks = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/blocks")
        ).json()["data"]
        text_blocks = [b for b in blocks if b["text"].strip()]
        assert text_blocks
        for b in text_blocks:
            assert b["x1"] <= page["width"] and b["y1"] <= page["height"]

    async def test_tables_endpoint(self, client):
        doc = await upload_extracted(client, fx.with_table())
        tables = (await client.get(f"/api/documents/{doc['id']}/tables")).json()["data"]
        assert tables
        t = tables[0]
        assert t["pageNumber"] == 1
        assert t["rowCount"] >= 3 and t["columnCount"] == 3
        assert "심박수" in t["markdownText"]

    async def test_caption_marked(self, client):
        doc = await upload_extracted(client, fx.image_with_caption())
        blocks = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/blocks")
        ).json()["data"]
        captions = [b for b in blocks if b["isCaption"]]
        assert captions and "그림 1" in captions[0]["text"]

    async def test_mixed_scan_partially_extracted(self, client):
        doc = await upload_extracted(client, fx.scanned_page_doc(text_pages=1, scanned_pages=1))
        assert doc["processingStatus"] == "partially_extracted"
        status = (
            await client.get(f"/api/documents/{doc['id']}/extraction-status")
        ).json()["data"]
        assert status["pagesDone"] == 2
        assert status["pagesOcrRequired"] == 1
        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        verdicts = {p["pageNumber"]: p["scanVerdict"] for p in pages}
        assert verdicts[1] == "digital"
        assert verdicts[2] == "scanned"

    async def test_full_scan_requires_ocr(self, client):
        doc = await upload_extracted(client, fx.scanned_page_doc(text_pages=0, scanned_pages=2))
        assert doc["processingStatus"] == "ocr_required"

    async def test_image_only_six_page_document_all_pages_need_ocr(self, client):
        """실기기 회귀: 글자 선택이 안 되는 이미지 전용 PDF의 모든 페이지가
        requires_ocr=True로 판정되고, ocr-status가 전 페이지를 대상으로 잡아야 한다."""
        doc = await upload_extracted(
            client, fx.scanned_page_doc(text_pages=0, scanned_pages=6)
        )
        assert doc["processingStatus"] == "ocr_required"
        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        assert len(pages) == 6
        assert all(p["requiresOcr"] for p in pages)
        assert all(p["scanVerdict"] == "scanned" for p in pages)

        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert sorted(status["remainingOcrPages"]) == [1, 2, 3, 4, 5, 6]

    async def test_mixed_document_only_scanned_pages_are_ocr_targets(self, client):
        doc = await upload_extracted(
            client, fx.scanned_page_doc(text_pages=1, scanned_pages=2)
        )
        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        by_number = {p["pageNumber"]: p for p in pages}
        assert not by_number[1]["requiresOcr"]
        assert by_number[2]["requiresOcr"] and by_number[3]["requiresOcr"]

        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert sorted(status["remainingOcrPages"]) == [2, 3]

    async def test_reprocess_replaces_rows_without_duplicates(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        assert doc["processingStatus"] == "extracted"
        res = await client.post(f"/api/documents/{doc['id']}/extract/retry")
        assert res.status_code == 200
        await drain_jobs()
        async with get_session_factory()() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentPage)
                    .where(DocumentPage.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert count == 2  # 페이지 중복 없음

    async def test_page_preparation_does_not_hold_sqlite_writer_lock(
        self, client, monkeypatch
    ):
        """CPU 전처리 중에는 별도 연결이 BEGIN IMMEDIATE를 획득할 수 있어야 한다."""
        from app.services.extraction import pipeline as pipeline_mod

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        original_prepare = pipeline_mod._prepare_page_rows
        preparation_started = threading.Event()
        release_preparation = threading.Event()

        def paused_prepare(document_id, page):
            preparation_started.set()
            if not release_preparation.wait(timeout=10):
                raise TimeoutError("test did not release page preparation")
            return original_prepare(document_id, page)

        monkeypatch.setattr(pipeline_mod, "_prepare_page_rows", paused_prepare)
        retry = await client.post(f"/api/documents/{doc['id']}/extract/retry")
        assert retry.status_code == 200

        try:
            assert await asyncio.to_thread(preparation_started.wait, 5)
            async with get_session_factory()() as session:
                await session.execute(text("PRAGMA busy_timeout=100"))
                await session.execute(text("BEGIN IMMEDIATE"))
                await session.rollback()
        finally:
            release_preparation.set()

        await drain_jobs()
        final = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert final["processingStatus"] == "extracted"

    async def test_extract_skips_when_result_is_current(self, client):
        doc = await upload_extracted(client, fx.sparse_text())
        res = await client.post(f"/api/documents/{doc['id']}/extract")
        assert res.status_code == 200
        # 같은 엔진·스키마 결과가 있으므로 다시 extracting으로 가지 않는다
        assert res.json()["data"]["processingStatus"] != "extracting"

    async def test_cancel_returns_to_ready(self, client, monkeypatch):
        from app.services.extraction import pipeline as pipeline_mod

        # 페이지 처리 지연을 흉내내 취소 시점을 확보
        original = pipeline_mod.engine_mod.extract_page

        def slow_extract(doc, index):
            import time

            time.sleep(0.15)
            return original(doc, index)

        monkeypatch.setattr(pipeline_mod.engine_mod, "extract_page", slow_extract)

        res = await client.post(
            "/api/documents", **upload_kwargs(fx.single_column_korean(pages=4))
        )
        doc_id = res.json()["data"]["id"]
        # 검증이 끝나고 추출이 시작될 때까지 잠깐 대기
        for _ in range(100):
            status = (await client.get(f"/api/documents/{doc_id}")).json()["data"][
                "processingStatus"
            ]
            if status == "extracting":
                break
            await asyncio.sleep(0.03)
        assert status == "extracting"

        cancel = await client.post(f"/api/documents/{doc_id}/extract/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["data"]["processingStatus"] == "ready"
        await drain_jobs()

        # 취소 후 재실행하면 정상 완료
        await client.post(f"/api/documents/{doc_id}/extract")
        await drain_jobs()
        final = (await client.get(f"/api/documents/{doc_id}")).json()["data"]
        assert final["processingStatus"] == "extracted"

    async def test_delete_cascades_extraction_rows(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        doc_uuid = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            words_before = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentWord)
                    .join(DocumentPage, DocumentPage.id == DocumentWord.page_id)
                    .where(DocumentPage.document_id == doc_uuid)
                )
            ).scalar_one()
        assert words_before > 0

        assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 200
        async with get_session_factory()() as session:
            pages_after = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentPage)
                    .where(DocumentPage.document_id == doc_uuid)
                )
            ).scalar_one()
            blocks_after = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentBlock)
                    .where(DocumentBlock.document_id == doc_uuid)
                )
            ).scalar_one()
        assert pages_after == 0 and blocks_after == 0

    async def test_restart_recovery_of_interrupted_extraction(self, client):
        from app.services.tasks.runner import get_task_runner

        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        doc_id = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            row = await session.get(Document, doc_id)
            assert row is not None
            row.processing_status = ProcessingStatus.EXTRACTING  # 중단 재현
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == doc_id,
                        DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
            job.status = JobStatus.RUNNING
            job.attempt_count = job.max_attempts - 1
            await session.commit()

        await get_task_runner().recover_interrupted()
        await drain_jobs()
        final = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert final["processingStatus"] == "extracted"
        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == doc_id,
                        DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
            assert job.status == JobStatus.SUCCEEDED
            assert job.attempt_count == job.max_attempts

    async def test_restart_recovery_does_not_exceed_max_attempts(self, client):
        from app.services.tasks.runner import get_task_runner

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        doc_id = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            row = await session.get(Document, doc_id)
            assert row is not None
            row.processing_status = ProcessingStatus.EXTRACTING
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == doc_id,
                        DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
            job.status = JobStatus.RUNNING
            job.attempt_count = job.max_attempts
            await session.commit()

        recovered = await get_task_runner().recover_interrupted()
        assert recovered == 0

        async with get_session_factory()() as session:
            row = await session.get(Document, doc_id)
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == doc_id,
                        DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
            assert row is not None
            assert row.processing_status == ProcessingStatus.EXTRACTION_FAILED
            assert row.failure_code == "EXTRACTION_MAX_ATTEMPTS"
            assert job.status == JobStatus.FAILED
            assert job.failure_code == "EXTRACTION_MAX_ATTEMPTS"
            assert job.attempt_count == job.max_attempts


class TestReprocessFailureIntegrity:
    async def test_prepare_failure_replaces_old_rows_with_failed_page(
        self, client, monkeypatch
    ):
        """재처리 준비 실패도 이전 결과를 FAILED 페이지로 원자적으로 교체한다."""
        from app.services.extraction import pipeline as pipeline_mod

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))

        def boom(document_id, page):
            raise RuntimeError("prepare boom")

        monkeypatch.setattr(pipeline_mod, "_prepare_page_rows", boom)
        await client.post(f"/api/documents/{doc['id']}/extract/retry")
        await drain_jobs()

        detail = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert detail["processingStatus"] == "extraction_failed"
        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        assert len(pages) == 1
        assert pages[0]["extractionStatus"] == "failed"

    async def test_persist_failure_replaces_old_rows_with_failed_page(self, client, monkeypatch):
        """재처리 중 저장 실패 시 이전 결과가 남지 않고 FAILED 페이지로 교체된다."""
        from app.services.extraction import pipeline as pipeline_mod

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        assert doc["processingStatus"] == "extracted"

        async def boom(session, document_id, page):
            raise RuntimeError("persist boom")

        monkeypatch.setattr(pipeline_mod, "_persist_page", boom)
        await client.post(f"/api/documents/{doc['id']}/extract/retry")
        await drain_jobs()
        monkeypatch.undo()

        detail = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert detail["processingStatus"] == "extraction_failed"
        pages = (await client.get(f"/api/documents/{doc['id']}/pages")).json()["data"]
        assert len(pages) == 1
        assert pages[0]["extractionStatus"] == "failed"  # 옛 성공 결과가 남아있지 않다
