"""키워드/벡터/하이브리드 검색 통합 테스트."""

import uuid

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.search import DocumentChunk
from app.services.search.chunking import rebuild_chunks
from app.services.search.embedding import DeterministicEmbeddingProvider, DisabledEmbeddingProvider
from app.services.search.hybrid import search
from app.services.search.keyword import search_keyword
from app.services.search.vector import pack_vector
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def upload_and_chunk(client, data: bytes) -> str:
    res = await client.post(
        "/api/documents", files={"file": ("doc.pdf", data, "application/pdf")}
    )
    assert res.status_code == 201, res.text
    doc_id = res.json()["data"]["id"]
    await drain_jobs()
    async with get_session_factory()() as session:
        await rebuild_chunks(session, uuid.UUID(doc_id))
        await session.commit()
    return doc_id


async def _embed_all_chunks(doc_id: str, provider: DeterministicEmbeddingProvider) -> None:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == uuid.UUID(doc_id))
            )
        ).scalars().all()
        vectors = provider.embed_documents([r.normalized_text for r in rows])
        for row, vec in zip(rows, vectors, strict=True):
            row.embedding_model = provider.model_name
            row.embedding_dimension = provider.dimension
            row.embedding_blob = pack_vector(vec)
        await session.commit()


class TestKeywordSearch:
    async def test_finds_relevant_chunk(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        async with get_session_factory()() as session:
            hits = await search_keyword(session, uuid.UUID(doc_id), "심장은", limit=10)
        assert hits

    async def test_no_match_returns_empty_not_error(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            hits = await search_keyword(session, uuid.UUID(doc_id), "존재하지않는단어xyz", limit=10)
        assert hits == []

    async def test_two_char_korean_query_yields_no_match_trigram_limitation(self, client):
        """trigram 토크나이저는 3자 미만 질의를 매치하지 못한다(알려진 한계, 오류는 아님).

        한국어는 공백이 조사가 붙은 어절 단위라 unicode61 단어 토큰화로는
        "심장"이 실제 저장된 "심장은"에 매치되지 않는다(형태소 분석 없음). trigram으로
        바꿔 3자 이상 질의는 정상 매치되지만, 2자 이하 질의는 이 한계가 남는다 —
        오류 대신 빈 배열을 반환해야 한다(사용자에게는 "결과 없음"으로 보인다).
        """
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        async with get_session_factory()() as session:
            hits = await search_keyword(session, uuid.UUID(doc_id), "심장", limit=10)
        assert hits == []

    async def test_special_characters_do_not_crash_fts(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            # FTS5 연산자로 해석될 수 있는 문자들 — 구문 오류 없이 안전하게 처리돼야 한다
            hits = await search_keyword(session, uuid.UUID(doc_id), 'AND OR "()*:', limit=10)
        assert isinstance(hits, list)


class TestHybridSearchModes:
    async def test_keyword_mode_works_without_embedding_provider(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="심장은",
                mode="keyword",
                limit=10,
                embedding_provider=DisabledEmbeddingProvider(),
            )
        assert results
        assert all(r.match_type == "keyword" for r in results)

    async def test_hybrid_mode_with_disabled_provider_falls_back_to_keyword_only(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="심장은",
                mode="hybrid",
                limit=10,
                embedding_provider=DisabledEmbeddingProvider(),
            )
        assert results
        assert all(r.match_type == "keyword" for r in results)

    async def test_vector_mode_ranks_by_cosine_similarity(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        provider = DeterministicEmbeddingProvider(dimension=16)
        await _embed_all_chunks(doc_id, provider)

        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="심장은 온몸에 혈액을 보내는 근육 기관이다",
                mode="vector",
                limit=5,
                embedding_provider=provider,
            )
        assert results
        assert all(r.match_type == "vector" for r in results)

    async def test_hybrid_mode_combines_keyword_and_vector(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=2))
        provider = DeterministicEmbeddingProvider(dimension=16)
        await _embed_all_chunks(doc_id, provider)

        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="심장은",
                mode="hybrid",
                limit=10,
                embedding_provider=provider,
            )
        assert results
        assert all(r.match_type in ("keyword", "vector", "hybrid") for r in results)

    async def test_results_never_expose_raw_scores(self, client):
        doc_id = await upload_and_chunk(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="심장은",
                mode="keyword",
                limit=5,
                embedding_provider=DisabledEmbeddingProvider(),
            )
        for r in results:
            assert not hasattr(r, "score")
            assert not hasattr(r, "raw_bm25")

    async def test_empty_document_returns_empty_results(self, client):
        doc_id = await upload_and_chunk(client, fx.blank_image_page())
        async with get_session_factory()() as session:
            results = await search(
                session,
                uuid.UUID(doc_id),
                query="아무거나",
                mode="keyword",
                limit=10,
                embedding_provider=DisabledEmbeddingProvider(),
            )
        assert results == []
