"""추출 백그라운드 작업 — 페이지 독립 실패·진행률·취소·크래시 격리."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStatus
from app.services.documents.state_machine import transition
from app.services.extraction import engine as engine_mod
from app.services.extraction.pipeline import run_extraction

logger = get_logger(__name__)


async def _latest_job(session: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    stmt = (
        select(DocumentJob)
        .where(
            DocumentJob.document_id == document_id,
            DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
        )
        .order_by(DocumentJob.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def extract_document(document_id: uuid.UUID, correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()

    async with factory() as session:
        doc = await session.get(Document, document_id)
        if (
            doc is None
            or doc.deleted_at is not None
            or doc.processing_status != ProcessingStatus.EXTRACTING
        ):
            logger.info("extract_skip", document_id=str(document_id))
            return
        job = await _latest_job(session, document_id)
        job_id = job.id if job is not None else None
        if job is not None:
            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = datetime.now(UTC)
        await session.commit()

    summary = await run_extraction(factory, document_id, job_id)

    async with factory() as session:
        doc = await session.get(Document, document_id)
        job = await _latest_job(session, document_id)
        if doc is None or doc.deleted_at is not None:
            return
        if summary is None:
            # 취소됨 — 상태는 cancel API가 이미 되돌렸다
            if job is not None and job.status == JobStatus.RUNNING:
                job.status = JobStatus.FAILED
                job.failure_code = "CANCELLED"
                job.completed_at = datetime.now(UTC)
            await session.commit()
            logger.info("extract_cancelled", document_id=str(document_id))
            return
        if doc.processing_status != ProcessingStatus.EXTRACTING:
            return  # 그 사이 상태가 바뀜 (삭제 등)
        if job_id is not None and (job is None or job.id != job_id):
            return  # 새 실행으로 교체됨 — 최종 상태는 새 실행이 결정한다

        final = summary.final_status
        transition(doc, final)
        doc.processing_progress = 100
        doc.extraction_engine = engine_mod.ENGINE_NAME
        doc.extraction_engine_version = engine_mod.ENGINE_VERSION
        doc.extraction_schema_version = engine_mod.SCHEMA_VERSION
        doc.extraction_completed_at = datetime.now(UTC)
        if final == ProcessingStatus.EXTRACTION_FAILED:
            doc.failure_code = "EXTRACTION_FAILED"
            doc.failure_message = (
                "문서 내용을 읽지 못했습니다. 다시 시도하거나 다른 파일을 확인해 주세요."
            )
        if job is not None:
            job.status = (
                JobStatus.FAILED
                if final == ProcessingStatus.EXTRACTION_FAILED
                else JobStatus.SUCCEEDED
            )
            job.completed_at = datetime.now(UTC)
        await session.commit()
        logger.info(
            "extract_done",
            document_id=str(document_id),
            status=final.value,
            pages=summary.total_pages,
            ocr=summary.ocr_required,
            failed=summary.failed,
        )


async def mark_extraction_crashed(document_id: uuid.UUID) -> None:
    """작업 크래시 복구 — extracting 고착 방지."""
    async with get_session_factory()() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.processing_status != ProcessingStatus.EXTRACTING:
            return
        transition(doc, ProcessingStatus.EXTRACTION_FAILED)
        doc.failure_code = "EXTRACTION_FAILED"
        doc.failure_message = "문서 내용을 읽는 중 문제가 발생했습니다. 다시 시도해 주세요."
        job = await _latest_job(session, document_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.failure_code = "EXTRACTION_FAILED"
            job.completed_at = datetime.now(UTC)
        await session.commit()
