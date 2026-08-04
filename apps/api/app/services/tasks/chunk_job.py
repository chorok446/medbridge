"""청크 재생성 작업 — 문서 전체를 한 번에 재계산하는 단일 배치(페이지별 재개 없음).

크래시 시 재개 지점이 없으므로 실패로 확정한다(OCR stale job 정리와 동일 원칙) —
사용자가 다시 재생성을 요청하면 된다.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType
from app.services.search.chunking import LowConfidenceOnlyDocument, rebuild_chunks

logger = get_logger(__name__)


async def run_chunk_rebuild_job(document_id: uuid.UUID, correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()

    async with factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            return
        job = (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.CHUNK_REBUILD,
                )
                .order_by(DocumentJob.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if job is None:
            return
        job_id = job.id
        job.status = JobStatus.RUNNING
        job.attempt_count += 1
        job.started_at = datetime.now(UTC)
        await session.commit()

    try:
        async with factory() as session:
            chunk_count = await rebuild_chunks(session, document_id)
            await session.commit()
    except LowConfidenceOnlyDocument as exc:
        # 성공으로 마감하면 문서가 '준비됐지만 비어 있는' 상태로 굳고, 화면은 눌러도
        # 소용없는 "문서 검색 준비하기"만 반복해 권하게 된다.
        async with factory() as session:
            job = await session.get(DocumentJob, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.failure_code = "CHUNK_LOW_CONFIDENCE_ONLY"
                job.failure_reason = "low_confidence_only"
                job.completed_at = datetime.now(UTC)
            await session.commit()
        logger.warning(
            "chunk_rebuild_low_confidence_only",
            document_id=str(document_id),
            suppressed_blocks=exc.suppressed_blocks,
        )
        return

    async with factory() as session:
        job = await session.get(DocumentJob, job_id)
        if job is not None:
            job.status = JobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
        await session.commit()
    logger.info("chunk_rebuild_done", document_id=str(document_id), chunk_count=chunk_count)


async def mark_chunk_rebuild_crashed(document_id: uuid.UUID) -> None:
    """크래시 경계 — RUNNING/QUEUED로 남은 잡을 실패로 확정해 영구 차단을 막는다."""
    factory = get_session_factory()
    async with factory() as session:
        job = (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.CHUNK_REBUILD,
                )
                .order_by(DocumentJob.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            job.status = JobStatus.FAILED
            job.failure_code = "CHUNK_REBUILD_CRASHED"
            job.completed_at = datetime.now(UTC)
            await session.commit()
