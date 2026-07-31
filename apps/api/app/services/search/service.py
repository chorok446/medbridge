"""검색 API의 서비스 계층 — 청크 재생성 시작/상태 조회, 검색 실행."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType
from app.models.search import DocumentChunk
from app.services.search.embedding import get_embedding_provider
from app.services.search.hybrid import SearchResult
from app.services.search.hybrid import search as run_hybrid_search
from app.services.search.settings import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT

SEARCH_MODES = ("keyword", "vector", "hybrid")


async def _latest_chunk_job(db: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    return (
        await db.execute(
            select(DocumentJob)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.CHUNK_REBUILD,
            )
            .order_by(DocumentJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def start_chunk_rebuild(
    db: AsyncSession, doc: Document, correlation_id: str
) -> tuple[bool, uuid.UUID | None]:
    """재생성 잡을 등록한다. 반환: (started, job_id). 이미 진행 중이면 (False, 기존 job_id)."""
    from app.services.tasks.runner import get_task_runner

    running = await _latest_chunk_job(db, doc.id)
    if running is not None and running.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return False, running.id

    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.CHUNK_REBUILD,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    get_task_runner().enqueue_chunk_rebuild(doc.id, correlation_id)
    return True, job.id


@dataclass
class ChunkStatus:
    chunk_count: int
    last_rebuilt_at: datetime | None
    job_status: str | None


async def get_chunk_status(db: AsyncSession, doc: Document) -> ChunkStatus:
    chunk_count = (
        await db.execute(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc.id
            )
        )
    ).scalar_one()
    job = await _latest_chunk_job(db, doc.id)
    return ChunkStatus(
        chunk_count=chunk_count,
        last_rebuilt_at=job.completed_at if job and job.status == JobStatus.SUCCEEDED else None,
        job_status=job.status.value if job else None,
    )


async def search_document(
    db: AsyncSession, doc: Document, *, query: str, mode: str, limit: int
) -> list[SearchResult]:
    if not query or not query.strip():
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "검색어를 입력해 주세요.", status_code=422
        )
    if mode not in SEARCH_MODES:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "지원하지 않는 검색 방식입니다.", status_code=422
        )
    bounded_limit = max(1, min(limit, MAX_SEARCH_LIMIT)) if limit else DEFAULT_SEARCH_LIMIT

    provider = get_embedding_provider()
    return await run_hybrid_search(
        db,
        doc.id,
        query=query.strip(),
        mode=mode,
        limit=bounded_limit,
        embedding_provider=provider,
    )
