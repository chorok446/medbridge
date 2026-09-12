"""합성 자료로 자연어 검색 잡음·분절 문맥·활성 세대 경계를 재현한다."""

import hashlib
import uuid

import pytest
from sqlalchemy import text

from app.db.session import get_session_factory
from app.models.document import Document
from app.models.search import DocumentChunk, DocumentChunkGeneration
from app.services.qa import context
from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS, CONTEXT_MAX_CHARS, CONTEXT_MAX_CHUNKS
from app.services.search.embedding import DeterministicEmbeddingProvider, DisabledEmbeddingProvider
from app.services.search.keyword import search_keyword
from tests.integration.conftest import drain_jobs


@pytest.fixture
async def document_id(client, pdf_bytes, monkeypatch):
    monkeypatch.setattr(context, "get_embedding_provider", DisabledEmbeddingProvider)
    response = await client.post(
        "/api/documents", files={"file": ("synthetic.pdf", pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 201
    await drain_jobs()
    return uuid.UUID(response.json()["data"]["id"])


async def add_chunk(db, doc_id, index, body, *, title=None, pages=(1,), generation=None):
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        document_id=doc_id,
        generation_id=generation,
        chunk_index=index,
        normalized_text=body,
        section_title=title,
        page_start=min(pages),
        page_end=max(pages),
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        source_refs_json=[
            {
                "pageNumber": page,
                "blockId": str(uuid.uuid4()),
                "bbox": [0, index, 100, index + 1],
                "readingOrder": index,
                "sourceMethod": "digital",
            }
            for page in pages
        ],
    )
    db.add(chunk)
    await db.flush()
    await db.execute(
        text(
            "INSERT INTO document_chunks_fts "
            "(chunk_id, document_id, normalized_text, section_title) "
            "VALUES (:id, :doc, :body, :title)"
        ),
        {
            "id": str(chunk.id),
            "doc": str(doc_id) if generation is None else f"shadow:{generation}",
            "body": body,
            "title": title or "",
        },
    )
    return chunk


async def add_generation(db, doc_id, *, active):
    generation_id = uuid.uuid4()
    generation = DocumentChunkGeneration(
        id=generation_id,
        document_id=doc_id,
        source_revision=1,
        shadow_document_id=f"shadow:{generation_id}",
        status="active" if active else "building",
    )
    db.add(generation)
    await db.flush()
    if active:
        document = await db.get(Document, doc_id)
        assert document is not None
        document.active_chunk_generation_id = generation.id
        await db.flush()
    return generation.id


async def test_request_words_do_not_crowd_out_topic(document_id):
    async with get_session_factory()() as db:
        target = await add_chunk(db, document_id, 100, "심부전 학습용 정의입니다.")
        for index in range(40):
            await add_chunk(db, document_id, index, "한 " * 30 + f"정의 원문 문장 출처 {index}")
        result = await context.retrieve(
            db, document_id, "심부전의 정의를 설명하는 원문 한 문장과 출처를 보여주세요."
        )
        assert result.chunks
        assert result.chunks[0].chunk_id == str(target.id)
        assert len(result.chunks) == 1


async def test_unknown_topic_does_not_match_request_or_facet_words(document_id):
    async with get_session_factory()() as db:
        await add_chunk(db, document_id, 0, "한 문장으로 설명하는 다른 주제의 정의와 출처")
        result = await context.retrieve(
            db, document_id, "없는주제xyz의 정의를 한 문장으로 보여주세요."
        )
        assert result.chunks == []


async def test_request_only_question_has_no_search_fallback(document_id):
    async with get_session_factory()() as db:
        await add_chunk(db, document_id, 0, "원문 한 문장 출처를 보여주세요")
        result = await context.retrieve(db, document_id, "원문 한 문장과 출처를 보여주세요.")
        assert result.chunks == []


async def test_heading_includes_split_sentence_with_original_refs(document_id):
    async with get_session_factory()() as db:
        # 제목 다음의 메타데이터 때문에 본문이 6개 청크 뒤에서 시작하는 추출 형태.
        heading = await add_chunk(db, document_id, 100, "심부전\nCHAPTER", title="심부전")
        for index in range(101, 106):
            await add_chunk(db, document_id, index, f"합성 메타데이터 {index}")
        fragments = [
            await add_chunk(db, document_id, 106 + offset, body)
            for offset, body in enumerate(
                ["예제 장치의 첫 번째 부품이", "두 번째 부품과 연결되어", "하나의 회로를 이룹니다."]
            )
        ]
        result = await context.retrieve(db, document_id, "심부전의 정의를 보여주세요.")
        ids = [chunk.chunk_id for chunk in result.chunks]
        assert str(heading.id) in ids
        assert all(str(row.id) in ids for row in fragments)
        for row in fragments:
            ref = result.lookup[str(row.id)]
            assert ref.text == row.normalized_text
            assert ref.content_hash == row.content_hash
            assert ref.source_refs == [{**r, "sectionTitle": None} for r in row.source_refs_json]
        assert result.searched_ids == ids
        assert dict(result.chunk_hash_pairs) == {
            cid: ref.content_hash for cid, ref in result.lookup.items()
        }


async def test_neighbors_do_not_use_deduplicated_page_range_as_adjacency(document_id):
    async with get_session_factory()() as db:
        anchor = await add_chunk(db, document_id, 50, "심부전 설명", pages=(5,))
        near = await add_chunk(db, document_id, 49, "가까운 본문", pages=(5,))
        await add_chunk(db, document_id, 51, "범위만 겹치는 중복 제목", pages=(1, 10))
        await add_chunk(db, document_id, 52, "다른 페이지 본문", pages=(6,))
        await add_chunk(db, document_id, 100, "같은 페이지지만 먼 청크", pages=(5,))
        result = await context.retrieve(db, document_id, "심부전")
        assert set(result.lookup) == {str(anchor.id), str(near.id)}


@pytest.mark.parametrize("active_generation", [False, True])
async def test_two_character_search_and_neighbors_exclude_inactive_generation(
    document_id, active_generation
):
    async with get_session_factory()() as db:
        active_id = (
            await add_generation(db, document_id, active=True) if active_generation else None
        )
        hidden_id = await add_generation(db, document_id, active=False)
        target = await add_chunk(db, document_id, 20, "심장", generation=active_id)
        near = await add_chunk(db, document_id, 21, "활성 본문", generation=active_id)
        await add_chunk(db, document_id, 22, "심장 " * 40, generation=hidden_id)
        await add_chunk(db, document_id, 21, "숨긴 본문", generation=hidden_id)
        hits = await search_keyword(db, document_id, "심장", limit=8)
        assert [hit.chunk_id for hit in hits] == [target.id]
        result = await context.retrieve(db, document_id, "심장은 무엇인가요?")
        assert set(result.lookup) == {str(target.id), str(near.id)}


async def test_normal_search_keeps_and_semantics(document_id):
    async with get_session_factory()() as db:
        await add_chunk(db, document_id, 0, "심장 설명")
        assert await search_keyword(db, document_id, "심장 없는말", limit=8) == []


async def test_context_limits_and_deduplication_survive_expansion(document_id):
    async with get_session_factory()() as db:
        for index in range(40):
            await add_chunk(db, document_id, index, f"심장 {index} " + "내용 " * 1200)
        await add_chunk(db, document_id, 40, "심장 39 " + "내용 " * 1200)
        result = await context.retrieve(db, document_id, "심장")
        assert result.chunks
        assert sum(len(c.text) for c in result.chunks) <= CONTEXT_MAX_CHARS
        assert all(len(c.text) <= CHUNK_TEXT_MAX_CHARS for c in result.chunks)
        assert len({r.content_hash for r in result.lookup.values()}) == len(result.lookup)


async def test_short_chunks_have_a_count_limit(document_id):
    async with get_session_factory()() as db:
        for index in range(120):
            body = f"심장 {index}" if index % 15 == 0 else f"조각 {index}"
            await add_chunk(db, document_id, index, body)
        result = await context.retrieve(db, document_id, "심장")
        assert len(result.chunks) == CONTEXT_MAX_CHUNKS


async def test_hybrid_uses_clean_query_and_same_context_expansion(document_id, monkeypatch):
    monkeypatch.setattr(context, "get_embedding_provider", DeterministicEmbeddingProvider)
    async with get_session_factory()() as db:
        anchor = await add_chunk(db, document_id, 20, "심부전 합성 제목", title="심부전")
        near = await add_chunk(db, document_id, 21, "합성 본문")
        result = await context.retrieve(db, document_id, "심부전의 정의를 보여주세요.")
        assert result.retrieval_mode == "hybrid"
        assert set(result.lookup) == {str(anchor.id), str(near.id)}
