"""검색 API — 청크 재생성/상태/검색. 기술 용어(포트·모델명·원시 점수)는 노출하지 않는다."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.logging import correlation_id_var
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import CamelModel, Envelope, utc_isoformat
from app.services.documents.service import get_owned_document
from app.services.search import service as search_service
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["search"])


class ChunkRebuildOut(CamelModel):
    job_id: uuid.UUID | None
    started: bool


class ChunkStatusOut(CamelModel):
    chunk_count: int
    last_rebuilt_at: str | None
    job_status: str | None
    embedding_available: bool
    failure_code: str | None = None
    suppressed_pages: int = 0


class SearchRequest(CamelModel):
    query: str
    mode: str = Field(default="hybrid", pattern="^(keyword|vector|hybrid)$")
    limit: int = Field(default=10, ge=1, le=50)


class SourceRefOut(CamelModel):
    page_number: int
    block_id: str
    bbox: list[float]
    reading_order: int
    source_method: str


class SearchResultOut(CamelModel):
    chunk_id: uuid.UUID
    preview: str
    section_title: str | None
    page_start: int
    page_end: int
    source_refs: list[SourceRefOut]
    match_type: str


@router.post(
    "/{document_id}/chunks/rebuild",
    response_model=Envelope[ChunkRebuildOut],
    status_code=202,
)
async def rebuild_chunks_route(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    started, job_id = await search_service.start_chunk_rebuild(db, doc, correlation_id_var.get())
    return wrap(ChunkRebuildOut(job_id=job_id, started=started))


@router.get("/{document_id}/chunks/status", response_model=Envelope[ChunkStatusOut])
async def chunk_status_route(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    status = await search_service.get_chunk_status(db, doc)
    return wrap(
        ChunkStatusOut(
            chunk_count=status.chunk_count,
            last_rebuilt_at=utc_isoformat(status.last_rebuilt_at),
            job_status=status.job_status,
            embedding_available=status.embedding_available,
            failure_code=status.failure_code,
            suppressed_pages=status.suppressed_pages,
        )
    )


@router.post("/{document_id}/search", response_model=Envelope[list[SearchResultOut]])
async def search_route(
    document_id: uuid.UUID,
    body: SearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    results = await search_service.search_document(
        db, doc, query=body.query, mode=body.mode, limit=body.limit
    )
    return wrap(
        [
            SearchResultOut(
                chunk_id=r.chunk_id,
                preview=r.preview,
                section_title=r.section_title,
                page_start=r.page_start,
                page_end=r.page_end,
                source_refs=[SourceRefOut(**ref) for ref in r.source_refs],
                match_type=r.match_type,
            )
            for r in results
        ]
    )
