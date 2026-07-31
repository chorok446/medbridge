"""FTS5 키워드 검색 — 제목/섹션명에 가중치를 준다."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.search.settings import BODY_BM25_WEIGHT, SECTION_TITLE_BM25_WEIGHT


@dataclass
class KeywordHit:
    chunk_id: uuid.UUID
    raw_bm25: float  # SQLite bm25: 낮을수록(더 음수일수록) 더 좋은 매치


def _sanitize_fts_query(query: str) -> str:
    """사용자 입력을 FTS5 쿼리 문법이 아니라 일반 단어 목록으로 취급한다.

    각 단어를 큰따옴표로 감싸 리터럴 phrase로 만들어, 콜론·괄호 같은 FTS5
    연산자 문자가 포함돼도 구문 오류 없이 안전하게 검색된다.
    """
    terms = [t.strip() for t in query.split() if t.strip()]
    escaped = [t.replace('"', '""') for t in terms]
    return " ".join(f'"{t}"' for t in escaped)


async def search_keyword(
    session: AsyncSession, document_id: uuid.UUID, query: str, limit: int
) -> list[KeywordHit]:
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
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
                "doc_id": str(document_id),
                "limit": limit,
            },
        )
    ).all()
    return [KeywordHit(chunk_id=uuid.UUID(r.chunk_id), raw_bm25=r.score) for r in rows]
