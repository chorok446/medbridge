"""하이브리드 검색 — 키워드(FTS5 bm25) + 벡터(코사인) 점수를 정규화해 결합한다.

원시 점수(bm25 raw, cosine raw)는 API 응답에 절대 포함하지 않는다 — 최종 정렬
순서와 matchType만 노출한다.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search import DocumentChunk
from app.services.search.embedding import EmbeddingProvider
from app.services.search.keyword import search_keyword
from app.services.search.settings import (
    ADJACENT_PAGE_BONUS,
    KEYWORD_WEIGHT,
    SAME_PAGE_BONUS,
    SEARCH_PREVIEW_CHARS,
    VECTOR_WEIGHT,
)
from app.services.search.vector import SqliteChunkVectorStore, cosine_similarity

MatchType = str  # "keyword" | "vector" | "hybrid"


@dataclass
class SearchResult:
    chunk_id: uuid.UUID
    preview: str
    section_title: str | None
    page_start: int
    page_end: int
    source_refs: list[dict]
    match_type: MatchType


def _normalize(scores: dict[uuid.UUID, float], *, invert: bool) -> dict[uuid.UUID, float]:
    """min-max 정규화(0~1). invert=True면 낮을수록 좋은 원시 점수(bm25)를 뒤집는다."""
    if not scores:
        return {}
    values = list(scores.values())
    lo, hi = min(values), max(values)
    if hi == lo:
        return dict.fromkeys(scores, 1.0)

    def norm(v: float) -> float:
        n = (v - lo) / (hi - lo)
        return (1.0 - n) if invert else n

    return {k: norm(v) for k, v in scores.items()}


def _pages_overlap_or_adjacent(a: tuple[int, int], b: tuple[int, int]) -> str | None:
    a_start, a_end = a
    b_start, b_end = b
    if a_start <= b_end and b_start <= a_end:
        return "same"
    if a_start - b_end == 1 or b_start - a_end == 1:
        return "adjacent"
    return None


async def search(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    query: str,
    mode: str,
    limit: int,
    embedding_provider: EmbeddingProvider | None,
) -> list[SearchResult]:
    fetch_pool = max(limit * 3, 30)
    keyword_hits = await search_keyword(session, document_id, query, limit=fetch_pool)
    keyword_scores = _normalize({h.chunk_id: h.raw_bm25 for h in keyword_hits}, invert=True)

    vector_scores: dict[uuid.UUID, float] = {}
    if mode in ("vector", "hybrid") and embedding_provider and embedding_provider.available:
        query_vector = embedding_provider.embed_query(query)
        store = SqliteChunkVectorStore(session)
        candidates = await store.get_candidates(
            document_id,
            model_name=embedding_provider.model_name,
            dimension=embedding_provider.dimension,
        )
        raw_vector = {
            c.chunk_id: cosine_similarity(query_vector, c.vector) for c in candidates
        }
        # 코사인은 -1..1 — 키워드 점수(0..1)와 같은 축으로 맞춘다
        vector_scores = {k: (v + 1.0) / 2.0 for k, v in raw_vector.items()}

    all_ids = set(keyword_scores) | set(vector_scores)
    if not all_ids:
        return []

    base_scores: dict[uuid.UUID, float] = {}
    match_types: dict[uuid.UUID, MatchType] = {}
    for cid in all_ids:
        kw = keyword_scores.get(cid, 0.0)
        vec = vector_scores.get(cid, 0.0)
        in_kw, in_vec = cid in keyword_scores, cid in vector_scores
        if mode == "keyword":
            base_scores[cid] = kw
            match_types[cid] = "keyword"
        elif mode == "vector":
            base_scores[cid] = vec
            match_types[cid] = "vector"
        else:
            base_scores[cid] = kw * KEYWORD_WEIGHT + vec * VECTOR_WEIGHT
            if in_kw and in_vec:
                match_types[cid] = "hybrid"
            else:
                match_types[cid] = "keyword" if in_kw else "vector"

    chunk_rows = {
        row.id: row
        for row in (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.id.in_(all_ids))
            )
        ).scalars()
    }

    # 페이지 인접성 보너스 — 문서 하나 범위의 후보 집합(수십~수백 개)이라 O(n^2)로 충분하다.
    boosted_scores = dict(base_scores)
    for cid, chunk in chunk_rows.items():
        page_range = (chunk.page_start, chunk.page_end)
        bonus = 0.0
        for other_id, other in chunk_rows.items():
            if other_id == cid:
                continue
            relation = _pages_overlap_or_adjacent(page_range, (other.page_start, other.page_end))
            if relation == "same":
                bonus = max(bonus, SAME_PAGE_BONUS)
            elif relation == "adjacent":
                bonus = max(bonus, ADJACENT_PAGE_BONUS)
        boosted_scores[cid] = base_scores[cid] + bonus

    ranked_ids = sorted(boosted_scores, key=lambda cid: boosted_scores[cid], reverse=True)[:limit]

    results: list[SearchResult] = []
    for cid in ranked_ids:
        result_chunk = chunk_rows.get(cid)
        if result_chunk is None:
            continue
        chunk = result_chunk
        preview = chunk.normalized_text[:SEARCH_PREVIEW_CHARS]
        results.append(
            SearchResult(
                chunk_id=chunk.id,
                preview=preview,
                section_title=chunk.section_title,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                source_refs=chunk.source_refs_json,
                match_type=match_types[cid],
            )
        )
    return results
