"""문서 서비스 계층. 상태 전이·보상 처리는 전부 여기서만 수행한다."""

import base64
import uuid
from datetime import UTC, datetime

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStage, ProcessingStatus
from app.models.user import User
from app.services.documents import storage, validation
from app.services.documents.state_machine import transition

logger = get_logger(__name__)

_CHUNK = 1024 * 1024


async def _read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """크기 제한을 넘는 순간 즉시 중단한다. 전체를 읽은 뒤 검사하지 않는다."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE,
                f"파일이 최대 크기({max_bytes // (1024 * 1024)}MB)를 초과했습니다.",
                status_code=413,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _find_duplicate(db: AsyncSession, user_id: uuid.UUID, sha256: str) -> Document | None:
    stmt = (
        select(Document)
        .where(
            Document.user_id == user_id,
            Document.sha256 == sha256,
            Document.deleted_at.is_(None),
            Document.processing_status != ProcessingStatus.DELETED,
            # 원본 파일이 사라진 실패 문서는 중복으로 취급하지 않는다 (재업로드 허용)
            ~(
                (Document.processing_status == ProcessingStatus.FAILED)
                & Document.storage_key.is_(None)
            ),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _reject_if_updating() -> None:
    """업데이트 준비 중에는 새 문서 작업을 시작하지 않는다."""
    from app.services.system import runtime

    if runtime.is_updating():
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "업데이트를 준비하는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=503,
            retryable=True,
        )


def _enqueue_validate(document_id: uuid.UUID, correlation_id: str) -> None:
    from app.services.tasks.runner import get_task_runner

    get_task_runner().enqueue_validate(document_id, correlation_id)


async def create_document(
    db: AsyncSession,
    user: User,
    file: UploadFile,
    title: str | None,
    correlation_id: str,
) -> tuple[Document, bool]:
    """업로드 처리. 반환: (document, duplicate 여부).

    보상 처리:
    - DB 생성 후 객체 저장 실패 → 문서 failed, storage_key 비움
    - 객체 저장 후 DB 갱신 실패 → 업로드된 객체 삭제 시도
    - 큐 등록 실패 → 문서 failed(retryable)
    """
    from app.services.system import runtime

    _reject_if_updating()
    with runtime.operation():  # 업데이트 정지 지점 계산용 (검사 직후 같은 틱에 등록)
        return await _create_document_inner(db, user, file, title, correlation_id)


async def _create_document_inner(
    db: AsyncSession,
    user: User,
    file: UploadFile,
    title: str | None,
    correlation_id: str,
) -> tuple[Document, bool]:
    settings = get_settings()
    data = await _read_limited(file, settings.max_upload_bytes)
    validation.check_size(len(data), settings.max_upload_bytes)
    validation.check_pdf_signature(data[:8])
    sha256 = validation.compute_sha256(data)

    existing = await _find_duplicate(db, user.id, sha256)
    if existing is not None:
        return existing, True

    original_filename = file.filename or "document.pdf"
    doc = Document(
        user_id=user.id,
        title=title or original_filename,
        original_filename=original_filename,
        sha256=sha256,
        file_size=len(data),
        processing_status=ProcessingStatus.CREATED,
        processing_stage=ProcessingStage.UPLOAD,
        processing_progress=0,
    )
    db.add(doc)
    await db.flush()  # id 확보

    key = storage.object_key(doc.id)
    transition(doc, ProcessingStatus.UPLOADING)
    await db.commit()

    try:
        await run_in_threadpool(storage.put_original, key, data)
    except AppError:
        doc.storage_key = None
        transition(doc, ProcessingStatus.FAILED)
        doc.failure_code = ErrorCode.STORAGE_UPLOAD_FAILED
        doc.failure_message = "파일 저장에 실패했습니다. 재시도해 주세요."
        await db.commit()
        raise

    # storage_key·상태 전이·작업 생성을 단일 커밋으로 묶어 중간 상태가 남지 않게 한다
    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.VALIDATE_FILE,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    try:
        doc.storage_key = key
        transition(doc, ProcessingStatus.UPLOADED)
        transition(doc, ProcessingStatus.QUEUED)
        doc.processing_stage = ProcessingStage.FILE_VALIDATION
        db.add(job)
        await db.commit()
    except Exception:
        # DB 갱신 실패 보상: 저장한 파일 제거 + 문서를 실패로 확정 (재업로드 가능하게)
        await db.rollback()
        try:
            await run_in_threadpool(storage.delete_original, key)
        except Exception:
            logger.warning("compensation_delete_failed", document_id=str(doc.id))
        try:
            stranded = await db.get(Document, doc.id)  # uploading 상태로 커밋돼 있는 행
            if stranded is not None:
                transition(stranded, ProcessingStatus.FAILED)
                stranded.storage_key = None
                stranded.failure_code = ErrorCode.INTERNAL_ERROR
                stranded.failure_message = (
                    "업로드 처리 중 오류가 발생했습니다. 파일을 다시 업로드해 주세요."
                )
                await db.commit()
        except Exception:
            logger.error("compensation_mark_failed_failed", document_id=str(doc.id))
        raise

    try:
        _enqueue_validate(doc.id, correlation_id)
    except Exception:
        logger.error("enqueue_failed", document_id=str(doc.id))
        transition(doc, ProcessingStatus.FAILED)
        doc.failure_code = ErrorCode.QUEUE_ENQUEUE_FAILED
        doc.failure_message = "검증 작업 등록에 실패했습니다. 재시도해 주세요."
        job.status = JobStatus.FAILED
        job.failure_code = ErrorCode.QUEUE_ENQUEUE_FAILED
        await db.commit()

    return doc, False


async def get_owned_document(
    db: AsyncSession, user: User, document_id: uuid.UUID, *, include_deleted: bool = False
) -> Document:
    """모든 문서 접근의 단일 소유권 검사 지점."""
    doc = await db.get(Document, document_id)
    if doc is None or doc.user_id != user.id:
        # 존재 여부를 노출하지 않기 위해 타인 문서도 404로 응답
        raise AppError(ErrorCode.NOT_FOUND, "문서를 찾을 수 없습니다.", status_code=404)
    if not include_deleted and (
        doc.deleted_at is not None or doc.processing_status == ProcessingStatus.DELETED
    ):
        raise AppError(ErrorCode.NOT_FOUND, "문서를 찾을 수 없습니다.", status_code=404)
    return doc


def _encode_cursor(created_at: datetime, doc_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{doc_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, doc_id = raw.split("|", 1)
        return datetime.fromisoformat(ts), uuid.UUID(doc_id)
    except Exception as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "잘못된 cursor 값입니다.", status_code=422
        ) from exc


async def list_documents(
    db: AsyncSession,
    user: User,
    *,
    status: ProcessingStatus | None,
    search: str | None,
    cursor: str | None,
    limit: int = 20,
) -> tuple[list[Document], str | None]:
    stmt = (
        select(Document)
        .where(Document.user_id == user.id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        stmt = stmt.where(Document.processing_status == status)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Document.title.ilike(pattern) | Document.original_filename.ilike(pattern))
    if cursor:
        created_at, doc_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Document.created_at < created_at)
            | ((Document.created_at == created_at) & (Document.id < doc_id))
        )
    rows = list((await db.execute(stmt)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return rows, next_cursor


async def delete_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    doc = await get_owned_document(db, user, document_id)
    # idempotent: 이전 삭제가 파일 삭제 단계에서 실패해 deleting에 머문 경우 재시도 허용
    if doc.processing_status != ProcessingStatus.DELETING:
        transition(doc, ProcessingStatus.DELETING)
        await db.commit()

    if doc.storage_key:
        try:
            await run_in_threadpool(storage.delete_original, doc.storage_key)
        except Exception as exc:
            # 객체 삭제 실패 → DB를 지우지 않고 deleting 상태 유지 (재시도 가능)
            doc.failure_code = ErrorCode.STORAGE_DELETE_FAILED
            doc.failure_message = "저장소에서 파일 삭제에 실패했습니다. 다시 시도해 주세요."
            await db.commit()
            raise AppError(
                ErrorCode.STORAGE_DELETE_FAILED,
                "파일 삭제에 실패했습니다. 잠시 후 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

    # 파생 추출 데이터 정리 (soft delete라 FK cascade가 돌지 않으므로 명시 삭제;
    # pages 삭제가 blocks/lines/words/tables로 cascade된다)
    from sqlalchemy import delete as sa_delete

    from app.models.extraction import DocumentPage

    await db.execute(sa_delete(DocumentPage).where(DocumentPage.document_id == doc.id))
    transition(doc, ProcessingStatus.DELETED)
    doc.deleted_at = datetime.now(UTC)
    doc.storage_key = None
    doc.extraction_completed_at = None
    await db.commit()
    return doc


async def retry_document(
    db: AsyncSession, user: User, document_id: uuid.UUID, correlation_id: str
) -> Document:
    _reject_if_updating()
    doc = await get_owned_document(db, user, document_id)
    if doc.processing_status != ProcessingStatus.FAILED:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "실패 상태의 문서만 재시도할 수 있습니다.",
            status_code=409,
        )
    if doc.storage_key is None:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "원본 파일이 저장되지 않아 재시도할 수 없습니다. 다시 업로드해 주세요.",
            status_code=409,
        )
    transition(doc, ProcessingStatus.QUEUED)
    doc.processing_stage = ProcessingStage.FILE_VALIDATION
    doc.failure_code = None
    doc.failure_message = None
    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.VALIDATE_FILE,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    db.add(job)
    await db.commit()

    try:
        _enqueue_validate(doc.id, correlation_id)
    except Exception:
        transition(doc, ProcessingStatus.FAILED)
        doc.failure_code = ErrorCode.QUEUE_ENQUEUE_FAILED
        doc.failure_message = "검증 작업 등록에 실패했습니다. 재시도해 주세요."
        job.status = JobStatus.FAILED
        await db.commit()
    await db.refresh(doc)  # server-side updated_at 재로드 (직렬화 시 lazy IO 방지)
    return doc


async def rename_document(
    db: AsyncSession, user: User, document_id: uuid.UUID, title: str
) -> Document:
    doc = await get_owned_document(db, user, document_id)
    title = title.strip()
    if not title or len(title) > 500:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "제목은 1~500자여야 합니다.", status_code=422
        )
    doc.title = title
    await db.commit()
    await db.refresh(doc)
    return doc


async def list_jobs(db: AsyncSession, user: User, document_id: uuid.UUID) -> list[DocumentJob]:
    await get_owned_document(db, user, document_id, include_deleted=True)
    stmt = (
        select(DocumentJob)
        .where(DocumentJob.document_id == document_id)
        .order_by(DocumentJob.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars())

