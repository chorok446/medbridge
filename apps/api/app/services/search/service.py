"""검색 API의 서비스 계층 — 청크 재생성 시작/상태 조회, 검색 실행."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus
from app.models.extraction import DocumentPage
from app.models.search import DocumentChunk
from app.services.search.embedding import get_embedding_provider
from app.services.search.hybrid import SearchResult
from app.services.search.hybrid import search as run_hybrid_search
from app.services.search.settings import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT

SEARCH_MODES = ("keyword", "vector", "hybrid")


async def _latest_chunk_job(db: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    from app.services.tasks.jobs import latest_job

    return await latest_job(db, document_id, JobType.CHUNK_REBUILD)


async def _has_active_ocr_job(db: AsyncSession, document_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(DocumentJob.id)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.OCR_DOCUMENT,
                DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def start_chunk_rebuild(
    db: AsyncSession, doc: Document, correlation_id: str
) -> tuple[bool, uuid.UUID | None]:
    """재생성 잡을 등록한다. 반환: (started, job_id). 이미 진행 중이면 (False, 기존 job_id)."""
    from app.services.tasks.runner import get_task_runner

    if await _has_active_ocr_job(db, doc.id):
        raise AppError(
            ErrorCode.INVALID_STATE,
            "OCR 처리가 끝난 뒤 검색 준비를 다시 시도해 주세요.",
            status_code=409,
            retryable=True,
        )

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
    try:
        await db.commit()
    except IntegrityError:
        # 활성 잡 유니크 인덱스 — 동시 요청이 이미 시작함(멱등하게 처리)
        await db.rollback()
        running = await _latest_chunk_job(db, doc.id)
        return False, running.id if running else None
    await db.refresh(job)
    get_task_runner().enqueue_chunk_rebuild(doc.id, correlation_id)
    return True, job.id


@dataclass
class ChunkStatus:
    chunk_count: int
    last_rebuilt_at: datetime | None
    job_status: str | None
    embedding_available: bool
    # 청크가 0개인 이유. 이게 없으면 화면은 "글자가 없는 문서"와 "읽었지만 못 믿어서
    # 뺀 문서"에 똑같은 안내를 하게 되고, 후자에는 그 안내가 통하지 않는다.
    failure_code: str | None = None
    # 인식 품질이 낮아 청크에서 빠진 쪽 수. failure_code는 청크가 **0개**일 때만
    # 채워지므로, 40/45쪽이 빠져도 청크가 5쪽 분량 남아 있으면 화면에는 아무 표시도
    # 없이 "검색 준비 완료"가 된다. 사용자는 문서의 90%가 검색·질문·요약에서 보이지
    # 않는데 그 사실을 알 길이 없다.
    suppressed_pages: int = 0


async def get_chunk_status(db: AsyncSession, doc: Document) -> ChunkStatus:
    chunk_count = (
        await db.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == doc.id)
        )
    ).scalar_one()
    job = await _latest_chunk_job(db, doc.id)
    suppressed = (
        await db.execute(
            select(func.count())
            .select_from(DocumentPage)
            .where(
                DocumentPage.document_id == doc.id,
                DocumentPage.ocr_status == OcrRunStatus.OCR_LOW_CONFIDENCE.value,
            )
        )
    ).scalar_one()
    return ChunkStatus(
        chunk_count=chunk_count,
        last_rebuilt_at=job.completed_at if job and job.status == JobStatus.SUCCEEDED else None,
        job_status=job.status.value if job else None,
        embedding_available=get_embedding_provider().available,
        failure_code=job.failure_code if job and job.status == JobStatus.FAILED else None,
        suppressed_pages=suppressed,
    )


async def search_document(
    db: AsyncSession, doc: Document, *, query: str, mode: str, limit: int
) -> list[SearchResult]:
    if not query or not query.strip():
        raise AppError(ErrorCode.VALIDATION_FAILED, "검색어를 입력해 주세요.", status_code=422)
    if mode not in SEARCH_MODES:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "지원하지 않는 검색 방식입니다.", status_code=422
        )
    current_revisions = (
        await db.execute(
            select(Document.content_revision, Document.chunk_revision).where(
                Document.id == doc.id
            )
        )
    ).one()
    if await _has_active_ocr_job(db, doc.id) or current_revisions[0] != current_revisions[1]:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "문서 내용이 갱신 중입니다. 검색 준비가 끝난 뒤 다시 시도해 주세요.",
            status_code=409,
            retryable=True,
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
