"""청크 생성 — reading_order 순서, 페이지 경계 source_ref, bbox 보존,
디지털·OCR 중복, 재실행 누적 방지, content_hash 중복 방지."""

import uuid

from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.search import DocumentChunk
from app.services.extraction.ocr import OcrResult
from app.services.ocr import service as ocr_service
from app.services.search.chunking import rebuild_chunks
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs
from tests.integration.test_ocr_flow import fake_words


def upload_kwargs(data: bytes, filename: str = "doc.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


async def _rebuild(doc_id: str) -> int:
    async with get_session_factory()() as session:
        count = await rebuild_chunks(session, uuid.UUID(doc_id))
        await session.commit()
        return count


class TestChunkOrderAndSourceRefs:
    async def test_chunks_follow_reading_order_two_columns(self, client):
        doc = await upload_extracted(client, fx.two_column_english(pages=1))
        count = await _rebuild(doc["id"])
        assert count > 0

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars().all()
        joined = "\n".join(r.normalized_text for r in rows)
        # 왼쪽 열 전체가 오른쪽 열보다 먼저 나와야 한다 (읽기 순서 유지)
        assert joined.index("L0-0") < joined.index("R0-0")

    async def test_chunk_spanning_pages_keeps_all_source_refs(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        spanning = [r for r in rows if r.page_start != r.page_end]
        if spanning:
            chunk = spanning[0]
            pages_in_refs = {ref["pageNumber"] for ref in chunk.source_refs_json}
            assert chunk.page_start in pages_in_refs
            assert chunk.page_end in pages_in_refs
        # 모든 청크는 최소 1개의 source_ref를 가진다 (출처 없는 청크 금지)
        assert all(len(r.source_refs_json) >= 1 for r in rows)

    async def test_bbox_preserved_in_source_refs(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        for row in rows:
            for ref in row.source_refs_json:
                bbox = ref["bbox"]
                assert len(bbox) == 4
                x0, y0, x1, y1 = bbox
                assert x0 <= x1 and y0 <= y1
                assert ref["sourceMethod"] in ("digital", "ocr")
                assert isinstance(ref["readingOrder"], int)

    async def test_table_kept_as_own_chunk_not_flattened(self, client):
        doc = await upload_extracted(client, fx.with_table())
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        table_chunks = [r for r in rows if "심박수" in r.normalized_text]
        assert table_chunks
        # 표 청크는 제목 블록("표가 있는 문서")과 섞이지 않는다 (평탄화 금지)
        assert "표가 있는 문서" not in table_chunks[0].normalized_text


class TestChunkDigitalOcrDedup:
    async def test_digital_preferred_over_ocr_on_same_page(self, client, monkeypatch):
        fake = type(
            "FakeEngine",
            (),
            {
                "available": True,
                "version": "fake-1.0",
                "recognize_page": lambda self, pdf_path, page_number, **kw: OcrResult(
                    page_number=page_number, words=fake_words(["OCR", "전용", "문장", "예시"])
                ),
            },
        )()
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(
            client, fx.mixed_digital_and_scanned()
        )
        # 스캔 페이지도 OCR 처리
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        digital_page_chunks = [r for r in rows if r.page_start == 1]
        for chunk in digital_page_chunks:
            assert all(ref["sourceMethod"] == "digital" for ref in chunk.source_refs_json)


class TestChunkRebuildIdempotency:
    async def test_rerun_does_not_accumulate(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        first_count = await _rebuild(doc["id"])
        second_count = await _rebuild(doc["id"])
        assert first_count == second_count

        async with get_session_factory()() as session:
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert total == first_count

    async def test_no_duplicate_content_hash_within_rebuild(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            hashes = [
                r[0]
                for r in (
                    await session.execute(
                        select(DocumentChunk.content_hash).where(
                            DocumentChunk.document_id == uuid.UUID(doc["id"])
                        )
                    )
                ).all()
            ]
        assert len(hashes) == len(set(hashes))

    async def test_delete_document_cascades_chunks(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        await _rebuild(doc["id"])
        assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 200

        async with get_session_factory()() as session:
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert remaining == 0
