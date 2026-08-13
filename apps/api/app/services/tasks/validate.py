"""파일 검증 작업 (구 dramatiq worker의 async 이식). idempotent."""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStatus
from app.services.documents import storage, validation
from app.services.documents.state_machine import transition
from app.services.tasks.jobs import latest_job

logger = get_logger(__name__)


async def _latest_job(session: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    return await latest_job(session, document_id, JobType.VALIDATE_FILE)


def _mark_failed(doc: Document, job: DocumentJob | None, code: str, message: str) -> None:
    transition(doc, ProcessingStatus.FAILED)
    doc.failure_code = code
    doc.failure_message = message
    if job is not None:
        job.status = JobStatus.FAILED
        job.failure_code = code
        job.failure_message = message
        job.completed_at = datetime.now(UTC)


async def mark_validation_crashed(document_id: uuid.UUID) -> None:
    """작업 크래시 복구 경로 — validating에 고착된 문서를 실패로 확정한다."""
    async with get_session_factory()() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.processing_status != ProcessingStatus.VALIDATING:
            return
        job = await _latest_job(session, document_id)
        _mark_failed(
            doc,
            job,
            ErrorCode.VALIDATION_FAILED,
            "파일을 확인하는 중 문제가 발생했습니다. 다시 시도해 주세요.",
        )
        await session.commit()


async def validate_document(document_id: uuid.UUID, correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)
    async with get_session_factory()() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            logger.info("validate_skip_deleted", document_id=str(document_id))
            return
        if doc.processing_status != ProcessingStatus.QUEUED:
            # 중복 실행 방지: queued가 아니면 이미 처리됐거나 처리 중
            logger.info(
                "validate_skip_status",
                document_id=str(document_id),
                status=doc.processing_status.value,
            )
            return

        job = await _latest_job(session, document_id)
        if job is not None:
            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = datetime.now(UTC)

        transition(doc, ProcessingStatus.VALIDATING)
        doc.processing_progress = 20
        await session.commit()

        try:
            if not doc.storage_key:
                _mark_failed(
                    doc,
                    job,
                    ErrorCode.VALIDATION_FAILED,
                    "저장된 파일을 찾을 수 없습니다. 파일을 다시 업로드해 주세요.",
                )
                await session.commit()
                return
            original_path = await asyncio.to_thread(storage.resolve_original_path, doc.storage_key)
            if not await asyncio.to_thread(original_path.is_file):
                _mark_failed(
                    doc,
                    job,
                    ErrorCode.VALIDATION_FAILED,
                    "저장된 파일을 찾을 수 없습니다. 파일을 다시 업로드해 주세요.",
                )
                await session.commit()
                return
        except Exception:
            _mark_failed(
                doc,
                job,
                ErrorCode.VALIDATION_FAILED,
                "파일을 읽는 중 오류가 발생했습니다. 다시 시도해 주세요.",
            )
            await session.commit()
            return

        doc.processing_progress = 60
        await session.commit()

        try:
            # PdfReader에는 저장소가 traversal 검사를 마친 경로의 file handle을 넘긴다.
            # 해시와 크기도 bounded chunk로 다시 계산해 저장 중 변조를 함께 검출한다.
            result = await asyncio.to_thread(validation.inspect_pdf_path, original_path)
            if result.sha256 != doc.sha256:
                raise AppError(
                    ErrorCode.VALIDATION_FAILED,
                    "저장된 파일이 업로드된 파일과 일치하지 않습니다.",
                )
            if result.file_size != doc.file_size:
                raise AppError(ErrorCode.VALIDATION_FAILED, "파일 크기가 일치하지 않습니다.")
        except AppError as exc:
            # 검증 판정 실패: 재시도해도 결과가 같으므로 즉시 실패 처리
            _mark_failed(doc, job, exc.code, exc.message)
            await session.commit()
            return

        doc.page_count = result.page_count
        transition(doc, ProcessingStatus.READY)
        doc.processing_progress = 100
        doc.failure_code = None
        doc.failure_message = None
        if job is not None:
            job.status = JobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
        await session.commit()
        logger.info("validate_ready", document_id=str(document_id), pages=result.page_count)

        # 검증 완료 → 본문 추출 자동 시작 (업로드→추출→검수 흐름을 클릭 없이 연결)
        try:
            from app.services.extraction.control import start_extraction

            await start_extraction(session, doc, correlation_id)
        except Exception:
            logger.warning("auto_extract_start_failed", document_id=str(document_id))
