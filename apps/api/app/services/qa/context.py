"""Q&A 컨텍스트 구성 — 현재 문서 범위 검색(기존 검색 서비스 재사용) + 모델 입력 청크.

검색은 hybrid(embedding 가능 시)/keyword(폴백)로 하고, 실제 retrieval mode를 메타데이터로
남긴다. 결과 chunk_id로 DocumentChunk 전체 텍스트·출처를 서버에서 재조회한다.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search import DocumentChunk
from app.services.qa.provider import QaContextChunk
from app.services.qa.query import keyword_query
from app.services.qa.settings import (
    CHUNK_TEXT_MAX_CHARS,
    CONTEXT_MAX_CHARS,
    CONTEXT_MAX_CHUNKS,
    QA_SEARCH_LIMIT,
)
from app.services.search.embedding import get_embedding_provider
from app.services.search.hybrid import search as run_search
from app.services.search.keyword import search_keyword


@dataclass
class QaChunkRef:
    """검증·출처 재구성용 — source_refs 스냅샷에 sectionTitle을 덧붙인다."""

    chunk_id: str
    section_title: str | None
    text: str
    content_hash: str
    source_refs: list[dict]


@dataclass
class RetrievalResult:
    chunks: list[QaContextChunk]
    lookup: dict[str, QaChunkRef]
    retrieval_mode: str
    searched_ids: list[str]
    chunk_hash_pairs: list[tuple[str, str]] = field(default_factory=list)


async def retrieve(db: AsyncSession, document_id: uuid.UUID, question: str) -> RetrievalResult:
    provider = get_embedding_provider()
    mode = "hybrid" if provider.available else "keyword"
    query = keyword_query(question)
    if not query:
        return RetrievalResult(chunks=[], lookup={}, retrieval_mode=mode, searched_ids=[])

    if mode == "hybrid":
        results = await run_search(
            db,
            document_id,
            query=query,
            mode=mode,
            limit=QA_SEARCH_LIMIT,
            embedding_provider=provider,
        )
        ordered_ids = [r.chunk_id for r in results]
    else:
        # 자연어 질문은 일부 단어만 겹쳐도 되므로 keyword를 OR로 검색한다(기존 검색은 AND 유지).
        hits = await search_keyword(db, document_id, query, limit=QA_SEARCH_LIMIT, match_all=False)
        ordered_ids = [h.chunk_id for h in hits]

    if not ordered_ids:
        return RetrievalResult(chunks=[], lookup={}, retrieval_mode=mode, searched_ids=[])

    # 검색 순위대로 DocumentChunk 전체 텍스트·출처를 재조회한다.
    # document_id도 함께 건다 — 다른 문서 청크가 섞이지 않게 하는 방어선(id는 이미
    # document_id 범위 검색 결과지만 재조회에서도 소속을 강제한다).
    rows = (
        (
            await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.id.in_(ordered_ids),
                    DocumentChunk.document_id == document_id,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    # 정확한 주제 제목을 우선한다. 짧은 제목만 반환하면 바로 뒤의 정의 본문을
    # 모델이 볼 수 없으므로 같은 쪽의 제한된 읽기 순서 범위를 함께 가져온다.
    terms = {term.casefold() for term in query.split()}
    anchors = [by_id[cid] for cid in ordered_ids if cid in by_id]
    if mode == "keyword":
        anchors.sort(key=lambda row: (row.section_title or "").strip().casefold() not in terms)
    expanded_rows: list[DocumentChunk] = []
    for anchor in anchors:
        neighbors = (
            (
                await db.execute(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.document_id == document_id,
                        DocumentChunk.generation_id == anchor.generation_id,
                        DocumentChunk.chunk_index.between(
                            anchor.chunk_index - 3, anchor.chunk_index + 8
                        ),
                    )
                    .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        pages = _source_pages(anchor)
        # 중복 제목의 page_start/end는 멀리 떨어진 출처를 아우를 수 있다.
        # 범위 겹침이 아니라 실제 source_ref의 페이지가 같은 경우만 확장한다.
        expanded_rows.extend(
            row
            for row in neighbors
            if row.id == anchor.id or pages.intersection(_source_pages(row))
        )

    chunks: list[QaContextChunk] = []
    lookup: dict[str, QaChunkRef] = {}
    seen_hashes: set[str] = set()
    total_chars = 0
    for row in expanded_rows:
        if len(chunks) >= CONTEXT_MAX_CHUNKS:
            break
        if row.content_hash in seen_hashes:  # near-dup 제거(동일 content_hash)
            continue
        text = (row.normalized_text or "")[:CHUNK_TEXT_MAX_CHARS]
        if not text.strip():
            continue
        if total_chars + len(text) > CONTEXT_MAX_CHARS and chunks:
            break  # 프롬프트 크기 상한(순위대로 절단)
        seen_hashes.add(row.content_hash)
        total_chars += len(text)
        sid = str(row.id)
        chunks.append(
            QaContextChunk(
                chunk_id=sid,
                section_title=row.section_title,
                text=text,
                page_start=row.page_start,
                page_end=row.page_end,
            )
        )
        # 출처 스냅샷에 sectionTitle을 덧붙여 보관한다
        refs = [{**ref, "sectionTitle": row.section_title} for ref in (row.source_refs_json or [])]
        lookup[sid] = QaChunkRef(
            chunk_id=sid,
            section_title=row.section_title,
            text=row.normalized_text or "",
            content_hash=row.content_hash,
            source_refs=refs,
        )

    hash_pairs = [(cid, lookup[cid].content_hash) for cid in lookup]
    return RetrievalResult(
        chunks=chunks,
        lookup=lookup,
        retrieval_mode=mode,
        searched_ids=list(lookup.keys()),
        chunk_hash_pairs=hash_pairs,
    )


def _source_pages(row: DocumentChunk) -> set[int]:
    return {
        ref["pageNumber"]
        for ref in (row.source_refs_json or [])
        if isinstance(ref.get("pageNumber"), int)
    }
