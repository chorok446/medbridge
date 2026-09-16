"""격리 DB에서 추출 메타데이터 저장부터 청크 재생성까지 검증한다."""

from uuid import UUID

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.extraction import DocumentBlock
from app.models.search import DocumentChunk
from app.services.extraction import pipeline
from app.services.search.chunking import rebuild_chunks
from tests.conftest import make_pdf
from tests.extraction_fixtures import numbered_section_page
from tests.integration.conftest import drain_jobs


async def test_persisted_section_boundary_splits_rebuilt_chunks(client, monkeypatch):
    # 파서 반환값만 합성한다. 이후 준비·저장·후처리·재조회·청크 생성은 제품 경로다.
    monkeypatch.setattr(
        pipeline.engine_mod, "extract_page",
        lambda document, index: numbered_section_page(index + 1),
    )
    response = await client.post("/api/documents", files={
        "file": ("synthetic-section.pdf", make_pdf(), "application/pdf"),
    })
    assert response.status_code == 201, response.text
    await drain_jobs()
    document_id = UUID(response.json()["data"]["id"])

    async with get_session_factory()() as session:
        blocks = list((await session.scalars(
            select(DocumentBlock).where(DocumentBlock.document_id == document_id)
            .order_by(DocumentBlock.block_index)
        )).all())
    assert len(blocks) == 8
    assert blocks[5].metadata_json == {"two_column": True, "section_boundary": True}
    assert all("section_boundary" not in b.metadata_json for b in blocks if b.block_index != 5)
    by_id = {str(b.id): b for b in blocks}

    for _ in range(2):
        async with get_session_factory()() as session:
            result = await rebuild_chunks(session, document_id)
            await session.commit()
        assert result.chunk_count == 2
        async with get_session_factory()() as session:
            chunks = list((await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
            )).all())
        assert len(chunks) == 2
        assert chunks[1].section_title == blocks[5].text
        assert [by_id[r["blockId"]].block_index for r in chunks[1].source_refs_json] == [5, 6, 7]
        refs = [ref for chunk in chunks for ref in chunk.source_refs_json]
        assert [by_id[r["blockId"]].block_index for r in refs] == [0, 1, 3, 2, 4, 5, 6, 7]
        for ref in refs:
            block = by_id[ref["blockId"]]
            assert ref["bbox"] == [block.x0, block.y0, block.x1, block.y1]
            assert ref["readingOrder"] == block.reading_order
            assert ref["pageNumber"] == 1 and ref["sourceMethod"] == "digital"
