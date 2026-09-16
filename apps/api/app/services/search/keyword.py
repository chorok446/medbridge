"""FTS5 키워드 검색 — 제목/섹션명에 가중치를 준다."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.search import DocumentChunk
from app.services.search.settings import BODY_BM25_WEIGHT, SECTION_TITLE_BM25_WEIGHT


@dataclass
class KeywordHit:
    chunk_id: uuid.UUID
    raw_bm25: float  # SQLite bm25: 낮을수록(더 음수일수록) 더 좋은 매치


def _sanitize_fts_query(query: str, *, match_all: bool = True) -> str:
    """사용자 입력을 FTS5 쿼리 문법이 아니라 일반 단어 목록으로 취급한다.

    각 단어를 큰따옴표로 감싸 리터럴 phrase로 만들어, 콜론·괄호 같은 FTS5
    연산자 문자가 포함돼도 구문 오류 없이 안전하게 검색된다. match_all=False면
    OR로 묶는다(자연어 질문처럼 일부 단어만 겹쳐도 되는 Q&A 검색용).
    """
    terms = [t.strip() for t in query.split() if t.strip()]
    escaped = [t.replace('"', '""') for t in terms]
    joiner = " " if match_all else " OR "
    return joiner.join(f'"{t}"' for t in escaped)


_MIN_TRIGRAM_TERM_CHARS = 3


async def search_keyword(
    session: AsyncSession,
    document_id: uuid.UUID,
    query: str,
    limit: int,
    *,
    match_all: bool = True,
) -> list[KeywordHit]:
    terms = [t.strip() for t in query.split() if t.strip()]
    if not terms:
        return []
    if any(len(t) < _MIN_TRIGRAM_TERM_CHARS for t in terms):
        # trigram 토크나이저는 3자 미만 검색어에서 토큰을 만들지 못해 매치가 전혀
        # 안 된다("심장", "혈압" 등 2글자 한국어 의학 용어가 흔하다) — 이런 경우만
        # 문서 범위 LIKE 스캔으로 대체한다. 개인용 앱 규모(문서 하나의 청크 수십~
        # 수백 개)라 선형 스캔으로 충분하다.
        return await _search_keyword_like(session, document_id, terms, limit, match_all=match_all)

    fts_query = _sanitize_fts_query(query, match_all=match_all)
    if not fts_query:
        return []
    active_generation_id = (
        await session.execute(
            select(Document.active_chunk_generation_id).where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    fts_document_id = (
        str(document_id) if active_generation_id is None else f"shadow:{active_generation_id}"
    )
    rows = (
        await session.execute(
            text(
                "SELECT chunk_id, bm25(document_chunks_fts, :body_w, :title_w) AS score "
                "FROM document_chunks_fts "
                "WHERE document_chunks_fts MATCH :fts_query AND document_id = :doc_id "
                "ORDER BY score LIMIT :limit"
            ),
            {
                "body_w": BODY_BM25_WEIGHT,
                "title_w": SECTION_TITLE_BM25_WEIGHT,
                "fts_query": fts_query,
                "doc_id": fts_document_id,
                "limit": limit,
            },
        )
    ).all()
    return [KeywordHit(chunk_id=uuid.UUID(r.chunk_id), raw_bm25=r.score) for r in rows]


async def _search_keyword_like(
    session: AsyncSession,
    document_id: uuid.UUID,
    terms: list[str],
    limit: int,
    *,
    match_all: bool = True,
) -> list[KeywordHit]:
    """bm25 점수는 없으므로 등장 횟수를 대체 점수로 쓴다(낮을수록 좋다는 bm25 관례에
    맞춰 음수로 뒤집는다). match_all=True면 모든 단어, False면 하나라도 포함된 청크."""
    columns = (DocumentChunk.id, DocumentChunk.normalized_text, DocumentChunk.section_title)
    rows = (
        await session.execute(select(*columns).where(DocumentChunk.document_id == document_id))
    ).all()
    lowered_terms = [t.lower() for t in terms]
    hits: list[KeywordHit] = []
    for row in rows:
        haystack = f"{row.normalized_text}\n{row.section_title or ''}".lower()
        present = [t for t in lowered_terms if t in haystack]
        if (match_all and len(present) < len(lowered_terms)) or not present:
            continue
        occurrences = sum(haystack.count(t) for t in present)
        hits.append(KeywordHit(chunk_id=row.id, raw_bm25=-float(occurrences)))
    hits.sort(key=lambda h: h.raw_bm25)
    return hits[:limit]
