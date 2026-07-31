"""요약 서비스 계층 — 시작/상태/조회/재시도/취소/삭제.

동시성은 document_jobs(job_type=summarize) 부분 유니크 인덱스로 DB 레벨 보장.
저장은 revision-guarded commit(시작·완료 시점 revision과 청크 해시가 같을 때만).
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.search import DocumentChunk
from app.models.summary import SummaryArtifact, SummaryRun
from app.services.summary.factory import get_summary_provider
from app.services.summary.pipeline import ChunkSnapshot
from app.services.summary.settings import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    VALID_LEARNER_LEVELS,
)


def compute_chunk_hash(content_hashes: list[str]) -> str:
    """청크 세트의 안정적 해시 — 정렬된 content_hash 목록 기준."""
    joined = "\n".join(sorted(content_hashes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def load_chunk_snapshots(
    db: AsyncSession, document_id: uuid.UUID
) -> list[ChunkSnapshot]:
    rows = (
        await db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
    ).scalars().all()
    return [
        ChunkSnapshot(
            chunk_id=str(r.id),
            section_title=r.section_title,
            text=r.normalized_text,
            page_start=r.page_start,
            page_end=r.page_end,
            source_refs=list(r.source_refs_json or []),
        )
        for r in rows
    ]


async def current_chunk_hash(db: AsyncSession, document_id: uuid.UUID) -> str:
    hashes = (
        await db.execute(
            select(DocumentChunk.content_hash).where(
                DocumentChunk.document_id == document_id
            )
        )
    ).scalars().all()
    return compute_chunk_hash(list(hashes))


async def _chunk_count(db: AsyncSession, document_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == document_id
            )
        )
    ).scalar_one()


async def _latest_run(db: AsyncSession, document_id: uuid.UUID) -> SummaryRun | None:
    return (
        await db.execute(
            select(SummaryRun)
            .where(SummaryRun.document_id == document_id)
            .order_by(SummaryRun.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def _latest_succeeded_run(
    db: AsyncSession, document_id: uuid.UUID
) -> SummaryRun | None:
    return (
        await db.execute(
            select(SummaryRun)
            .where(
                SummaryRun.document_id == document_id,
                SummaryRun.status == SummaryRunStatus.SUCCEEDED,
            )
            .order_by(SummaryRun.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def _active_summary_job(
    db: AsyncSession, document_id: uuid.UUID
) -> DocumentJob | None:
    return (
        await db.execute(
            select(DocumentJob)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.SUMMARIZE,
                DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
    ).scalars().first()


async def start_summary(
    db: AsyncSession,
    doc: Document,
    correlation_id: str,
    *,
    learner_level: str,
    language: str,
    include_sections: bool,
    include_prerequisites: bool,
) -> tuple[bool, uuid.UUID | None]:
    """요약 잡 등록. 반환 (started, run_id). 진행 중이면 (False, 기존 run_id)."""
    from app.services.tasks.runner import get_task_runner

    provider = await get_summary_provider(db)
    if not provider.available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "요약 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.",
            status_code=501,
        )
    if learner_level not in VALID_LEARNER_LEVELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 학습자 수준입니다.", status_code=422)

    # 청크 준비 여부 확인
    if await _chunk_count(db, doc.id) == 0:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "먼저 문서 검색 준비(청크 생성)를 완료해 주세요.",
            status_code=409,
        )
    if doc.chunk_revision != doc.content_revision:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "문서가 변경되어 검색 준비를 다시 해야 합니다. 잠시 후 다시 시도해 주세요.",
            status_code=409,
        )

    existing = await _active_summary_job(db, doc.id)
    if existing is not None:
        run = await _latest_run(db, doc.id)
        return False, run.id if run else None

    source_hash = await current_chunk_hash(db, doc.id)
    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.SUMMARIZE,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    run = SummaryRun(
        document_id=doc.id,
        status=SummaryRunStatus.QUEUED,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        source_revision=doc.content_revision,
        source_chunk_hash=source_hash,
        learner_level=learner_level,
        language=language,
    )
    db.add(job)
    db.add(run)
    try:
        await db.commit()
    except IntegrityError:
        # 부분 유니크 인덱스(활성 잡 1개)에 걸림 — 동시 요청이 이미 시작함
        await db.rollback()
        run = await _latest_run(db, doc.id)
        return False, run.id if run else None
    await db.refresh(run)
    get_task_runner().enqueue_summary(
        doc.id,
        correlation_id,
        run_id=run.id,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
    )
    return True, run.id


@dataclass
class SummaryStatus:
    provider_available: bool
    status: str | None
    stale: bool
    source_revision: int | None
    current_revision: int
    progress: int
    can_retry: bool


async def get_summary_status(db: AsyncSession, doc: Document) -> SummaryStatus:
    provider = await get_summary_provider(db)
    run = await _latest_run(db, doc.id)
    succeeded = await _latest_succeeded_run(db, doc.id)
    stale = bool(succeeded and succeeded.source_revision != doc.content_revision)
    status_val = run.status.value if run else None
    # 진행률: 러너가 세분 진행을 추적하지 않으므로 상태 기반 근사치
    progress = 0
    if run:
        progress = {
            SummaryRunStatus.QUEUED: 5,
            SummaryRunStatus.RUNNING: 50,
            SummaryRunStatus.SUCCEEDED: 100,
            SummaryRunStatus.FAILED: 0,
            SummaryRunStatus.CANCELLED: 0,
        }.get(run.status, 0)
    can_retry = bool(
        provider.available
        and (run is None or run.status in (SummaryRunStatus.FAILED, SummaryRunStatus.CANCELLED))
    )
    return SummaryStatus(
        provider_available=provider.available,
        status=status_val,
        stale=stale,
        source_revision=succeeded.source_revision if succeeded else None,
        current_revision=doc.content_revision,
        progress=progress,
        can_retry=can_retry,
    )


async def get_summary_artifacts(
    db: AsyncSession, doc: Document
) -> tuple[list[SummaryArtifact], bool]:
    """최신 성공 run의 artifact 목록 + stale 여부."""
    run = await _latest_succeeded_run(db, doc.id)
    if run is None:
        return [], False
    stale = run.source_revision != doc.content_revision
    artifacts = (
        await db.execute(
            select(SummaryArtifact)
            .where(SummaryArtifact.summary_run_id == run.id)
            .order_by(SummaryArtifact.position)
        )
    ).scalars().all()
    return list(artifacts), stale


async def cancel_summary(db: AsyncSession, doc: Document) -> None:
    job = await _active_summary_job(db, doc.id)
    if job is None:
        raise AppError(
            ErrorCode.INVALID_STATE, "지금은 취소할 요약 작업이 없습니다.", status_code=409
        )
    from datetime import UTC

    job.status = JobStatus.FAILED
    job.failure_code = "CANCELLED"
    job.completed_at = datetime.now(UTC)
    run = await _latest_run(db, doc.id)
    if run is not None and run.status in (SummaryRunStatus.QUEUED, SummaryRunStatus.RUNNING):
        run.status = SummaryRunStatus.CANCELLED
        run.error_code = "CANCELLED"
        run.completed_at = datetime.now(UTC)
    await db.commit()


async def delete_summaries(db: AsyncSession, doc: Document) -> None:
    """이 문서의 모든 요약 run·artifact 삭제 (soft-delete 문서라 명시 삭제)."""
    await db.execute(
        delete(SummaryArtifact).where(SummaryArtifact.document_id == doc.id)
    )
    await db.execute(delete(SummaryRun).where(SummaryRun.document_id == doc.id))
    await db.commit()
