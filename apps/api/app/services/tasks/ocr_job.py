"""OCR 백그라운드 작업 — 페이지 순차 처리(동시 1), job 토큰 CAS, 취소·복구."""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus
from app.models.extraction import DocumentPage
from app.models.ocr import OcrRun
from app.services.documents import storage
from app.services.ocr import service as ocr_service
from app.services.ocr.service import PENDING
from app.services.ocr.settings import QUALITY_DPI

logger = get_logger(__name__)


async def _job_is_current(session, document_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    latest = (
        await session.execute(
            select(DocumentJob)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.OCR_DOCUMENT,
            )
            .order_by(DocumentJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return (
        latest is not None
        and latest.id == job_id
        and latest.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    )


async def run_ocr_job(
    document_id: uuid.UUID,
    correlation_id: str,
    *,
    language: str = "kor+eng",
    quality: str = "standard",
) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()
    dpi = QUALITY_DPI.get(quality, QUALITY_DPI["standard"])

    async with factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None or doc.storage_key is None:
            return
        job = (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.OCR_DOCUMENT,
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
        pdf_path = str(storage.get_storage().resolve_path(doc.storage_key))

    eng = ocr_service.engine()
    processed = failed = 0
    while True:
        async with factory() as session:
            if not await _job_is_current(session, document_id, job_id):
                logger.info("ocr_job_superseded_or_cancelled", document_id=str(document_id))
                return
            page = (
                await session.execute(
                    select(DocumentPage)
                    .where(
                        DocumentPage.document_id == document_id,
                        DocumentPage.ocr_status == PENDING,
                    )
                    .order_by(DocumentPage.page_number)
                    .limit(1)
                )
            ).scalars().first()
            if page is None:
                break
            page.ocr_status = OcrRunStatus.RUNNING.value
            run = OcrRun(
                document_id=document_id,
                page_id=page.id,
                engine="tesseract",
                engine_version=eng.version,
                language=language,
                render_dpi=dpi,
                preprocessing_json={"grayscale": True},
            )
            session.add(run)
            await session.commit()
            page_number = page.page_number

        try:
            result = await asyncio.to_thread(
                eng.recognize_page, pdf_path, page_number, language=language, dpi=dpi
            )
            async with factory() as session:
                if not await _job_is_current(session, document_id, job_id):
                    return
                page = (
                    await session.execute(
                        select(DocumentPage).where(
                            DocumentPage.document_id == document_id,
                            DocumentPage.page_number == page_number,
                        )
                    )
                ).scalars().one()
                run_row = (
                    await session.execute(
                        select(OcrRun)
                        .where(OcrRun.page_id == page.id)
                        .order_by(OcrRun.created_at.desc())
                        .limit(1)
                    )
                ).scalars().one()
                await ocr_service.apply_ocr_result(session, page, result, run_row)
                await session.commit()
                processed += 1
                logger.info(
                    "ocr_page_done",
                    document_id=str(document_id),
                    page=page_number,
                    words=run_row.word_count,
                    mean_confidence=run_row.mean_confidence,
                    duration_ms=run_row.duration_ms,
                )
        except Exception as exc:
            failed += 1
            logger.warning(
                "ocr_page_failed",
                document_id=str(document_id),
                page=page_number,
                error=type(exc).__name__,
            )
            async with factory() as session:
                page = (
                    await session.execute(
                        select(DocumentPage).where(
                            DocumentPage.document_id == document_id,
                            DocumentPage.page_number == page_number,
                        )
                    )
                ).scalars().first()
                if page is not None:
                    page.ocr_status = OcrRunStatus.OCR_FAILED.value
                    failed_run = (
                        await session.execute(
                            select(OcrRun)
                            .where(OcrRun.page_id == page.id)
                            .order_by(OcrRun.created_at.desc())
                            .limit(1)
                        )
                    ).scalars().first()
                    if failed_run is not None:
                        failed_run.status = OcrRunStatus.OCR_FAILED
                        failed_run.error_code = type(exc).__name__[:50]
                        failed_run.completed_at = datetime.now(UTC)
                    await session.commit()

    async with factory() as session:
        if not await _job_is_current(session, document_id, job_id):
            return
        job = await session.get(DocumentJob, job_id)
        doc = await session.get(Document, document_id)
        if job is not None:
            job.status = JobStatus.SUCCEEDED if failed == 0 else JobStatus.FAILED
            if failed:
                job.failure_code = "OCR_PARTIAL_FAILURE"
            job.completed_at = datetime.now(UTC)
        if doc is not None:
            await ocr_service.rollup_document_status(session, doc)
        await session.commit()
        logger.info(
            "ocr_job_done", document_id=str(document_id), processed=processed, failed=failed
        )
