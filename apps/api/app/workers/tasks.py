"""백그라운드 작업. idempotent — 처리 완료/삭제된 문서는 조용히 건너뛴다."""

import uuid
from datetime import UTC, datetime

import dramatiq
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var, get_logger
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStatus
from app.services.documents import storage, validation
from app.services.documents.state_machine import transition
from app.workers.broker import setup_broker

setup_broker()

logger = get_logger(__name__)

_sync_engine = None
_sync_session_factory = None


def get_sync_session() -> Session:
    global _sync_engine, _sync_session_factory
    if _sync_session_factory is None:
        _sync_engine = create_engine(get_settings().database_url_sync, pool_pre_ping=True)
        _sync_session_factory = sessionmaker(_sync_engine)
    return _sync_session_factory()


class _RetryableJobError(Exception):
    """지수 백오프 재시도 대상 (인프라 오류). 검증 판정 실패는 여기 속하지 않는다."""


def _latest_job(db: Session, document_id: uuid.UUID) -> DocumentJob | None:
    stmt = (
        select(DocumentJob)
        .where(
            DocumentJob.document_id == document_id,
            DocumentJob.job_type == JobType.VALIDATE_FILE,
        )
        .order_by(DocumentJob.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


@dramatiq.actor(max_retries=3, min_backoff=2_000, max_backoff=60_000)
def validate_file(document_id: str, correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)
    doc_id = uuid.UUID(document_id)
    db = get_sync_session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None or doc.deleted_at is not None:
            logger.info("validate_skip_deleted", document_id=document_id)
            return
        if doc.processing_status != ProcessingStatus.QUEUED:
            # 중복 전달·재실행 방지: queued가 아니면 이미 처리됐거나 처리 중
            logger.info(
                "validate_skip_status",
                document_id=document_id,
                status=doc.processing_status.value,
            )
            return

        job = _latest_job(db, doc_id)
        if job is not None:
            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = datetime.now(UTC)

        transition(doc, ProcessingStatus.VALIDATING)
        doc.processing_progress = 20
        db.commit()

        try:
            if not doc.storage_key or not storage.original_exists(doc.storage_key):
                raise _RetryableJobError("original object missing")
            data = storage.get_original(doc.storage_key)
        except _RetryableJobError:
            raise
        except Exception as exc:
            raise _RetryableJobError("storage read failed") from exc

        doc.processing_progress = 60
        db.commit()

        try:
            result = validation.inspect_pdf(data)
            if result.sha256 != doc.sha256:
                raise AppError(
                    ErrorCode.VALIDATION_FAILED,
                    "저장된 파일이 업로드된 파일과 일치하지 않습니다.",
                )
            if len(data) != doc.file_size:
                raise AppError(ErrorCode.VALIDATION_FAILED, "파일 크기가 일치하지 않습니다.")
        except AppError as exc:
            # 검증 판정 실패: 재시도해도 결과가 같으므로 즉시 실패 처리
            _mark_failed(db, doc, job, exc.code, exc.message)
            return

        doc.page_count = result.page_count
        transition(doc, ProcessingStatus.READY)
        doc.processing_progress = 100
        doc.failure_code = None
        doc.failure_message = None
        if job is not None:
            job.status = JobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
        db.commit()
        logger.info("validate_ready", document_id=document_id, pages=result.page_count)

    except _RetryableJobError:
        db.rollback()
        _handle_retryable(db, doc_id)
    finally:
        db.close()


def _mark_failed(
    db: Session, doc: Document, job: DocumentJob | None, code: str, message: str
) -> None:
    transition(doc, ProcessingStatus.FAILED)
    doc.failure_code = code
    doc.failure_message = message
    if job is not None:
        job.status = JobStatus.FAILED
        job.failure_code = code
        job.failure_message = message
        job.completed_at = datetime.now(UTC)
    db.commit()
    logger.info("validate_failed", document_id=str(doc.id), code=code)


def _handle_retryable(db: Session, doc_id: uuid.UUID) -> None:
    """인프라 오류: 시도 횟수가 남았으면 dramatiq 재시도, 소진 시 failed 확정."""
    doc = db.get(Document, doc_id)
    if doc is None:
        return
    job = _latest_job(db, doc_id)
    attempts = job.attempt_count if job is not None else 1
    max_attempts = job.max_attempts if job is not None else 3
    if attempts >= max_attempts:
        if doc.processing_status == ProcessingStatus.VALIDATING:
            _mark_failed(
                db,
                doc,
                job,
                ErrorCode.VALIDATION_FAILED,
                "파일 검증 중 저장소 오류가 반복되었습니다. 재시도해 주세요.",
            )
        return
    # validating → queued는 허용 전이가 아니므로 상태 머신을 우회하지 않고
    # dramatiq 재실행을 위해 queued로 복귀시키는 대신 예외를 다시 던진다.
    if doc.processing_status == ProcessingStatus.VALIDATING:
        doc.processing_status = ProcessingStatus.QUEUED  # ponytail: 재시도 전용 내부 복귀 경로
        db.commit()
    raise _RetryableJobError("retrying")
