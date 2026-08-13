"""문서 서비스 계층. 상태 전이·보상 처리는 전부 여기서만 수행한다."""

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
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

# 같은 사용자가 동일 파일을 동시에 올릴 때 중복 조회와 문서 생성을 한 임계 구역으로
# 묶고, 단일 사용자 앱에서 여러 대형 업로드가 디스크를 동시에 포화시키지 않게 한다.
_upload_gate = asyncio.Semaphore(1)


@dataclass(frozen=True)
class _StagedUploadResult:
    staged: storage.StagedUpload
    file_size: int
    sha256: str


async def _stage_upload(file: UploadFile, max_bytes: int) -> _StagedUploadResult:
    """기존 multipart ``UploadFile``을 bounded staging 경로로 복사한다."""

    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(_CHUNK):
            yield chunk

    return await _stage_chunks(chunks(), max_bytes)


async def _stage_chunks(chunks: AsyncIterable[bytes], max_bytes: int) -> _StagedUploadResult:
    """ASGI body chunk를 bounded 크기로 앱 데이터 staging 파일에 기록한다.

    크기·PDF signature·SHA-256은 읽는 동안 계산하며 파일 전체를 Python heap에
    보관하지 않는다. 어떤 실패나 task 취소에서도 staging 파일을 즉시 정리한다.
    """
    staged = await run_in_threadpool(storage.create_staged_upload)
    digest = hashlib.sha256()
    head = bytearray()
    file_size = 0
    try:
        async for incoming in chunks:
            # ASGI server가 큰 body chunk를 주더라도 디스크 write와 일시 참조 크기는
            # 1MiB로 제한한다. bytes slice 하나만 살아 있으므로 파일 크기에 비례하지 않는다.
            for offset in range(0, len(incoming), _CHUNK):
                chunk = incoming[offset : offset + _CHUNK]
                if not chunk:
                    continue
                next_size = file_size + len(chunk)
                if next_size > max_bytes:
                    raise AppError(
                        ErrorCode.FILE_TOO_LARGE,
                        f"파일이 최대 크기({max_bytes // (1024 * 1024)}MB)를 초과했습니다.",
                        status_code=413,
                    )

                if len(head) < 8:
                    head.extend(chunk[: 8 - len(head)])
                    # PDF가 아니면 나머지 수백 MB를 받기 전에 거부한다.
                    if len(head) >= len(validation.PDF_SIGNATURE):
                        validation.check_pdf_signature(head)

                digest.update(chunk)
                await run_in_threadpool(staged.write, chunk)
                file_size = next_size

        validation.check_size(file_size, max_bytes)
        validation.check_pdf_signature(head)
        await run_in_threadpool(staged.seal)
        return _StagedUploadResult(
            staged=staged,
            file_size=file_size,
            sha256=digest.hexdigest(),
        )
    except BaseException:
        # CancelledError도 포함해 종료·업데이트 시 사용자 파일 조각을 남기지 않는다.
        try:
            storage.discard_staged_upload(staged)
        except Exception:
            logger.warning("staged_upload_cleanup_failed")
        raise


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

    runtime.reject_if_updating()
    # 대형 파일의 staging I/O와 동일 해시 중복 판정을 한 번에 하나씩 처리한다.
    async with _upload_gate:
        # 게이트 대기(await) 중 업데이트 준비가 시작됐을 수 있다 — 카운터 등록과 같은
        # 틱에서 재검사하지 않으면 wait_for_quiescence가 이 업로드를 못 보고 통과해
        # 백업에 없는 문서가 업데이트 도중 생긴다.
        runtime.reject_if_updating()
        with runtime.operation():  # 업데이트 정지 지점 계산용 (검사 직후 같은 틱에 등록)
            return await _create_document_inner(
                db,
                user,
                await _stage_upload(file, get_settings().max_upload_bytes),
                file.filename or "document.pdf",
                title,
                correlation_id,
            )


async def create_document_stream(
    db: AsyncSession,
    user: User,
    chunks: AsyncIterable[bytes],
    original_filename: str,
    title: str | None,
    correlation_id: str,
    *,
    declared_size: int | None = None,
) -> tuple[Document, bool]:
    """raw ``application/pdf`` body를 framework 임시파일 없이 직접 staging한다."""
    from app.services.system import runtime

    runtime.reject_if_updating()
    settings = get_settings()
    if declared_size is not None and declared_size > settings.max_upload_bytes:
        raise AppError(
            ErrorCode.FILE_TOO_LARGE,
            f"파일이 최대 크기({settings.max_upload_bytes // (1024 * 1024)}MB)를 초과했습니다.",
            status_code=413,
        )
    async with _upload_gate:
        runtime.reject_if_updating()
        with runtime.operation():
            upload = await _stage_chunks(chunks, settings.max_upload_bytes)
            if declared_size is not None and upload.file_size != declared_size:
                storage.discard_staged_upload(upload.staged)
                raise AppError(
                    ErrorCode.VALIDATION_FAILED,
                    "전송된 파일 크기가 선택한 파일과 일치하지 않습니다. 다시 시도해 주세요.",
                    status_code=400,
                )
            return await _create_document_inner(
                db, user, upload, original_filename, title, correlation_id
            )


async def _create_document_inner(
    db: AsyncSession,
    user: User,
    upload: _StagedUploadResult,
    original_filename: str,
    title: str | None,
    correlation_id: str,
) -> tuple[Document, bool]:
    try:
        existing = await _find_duplicate(db, user.id, upload.sha256)
        if existing is not None:
            return existing, True

        doc = Document(
            user_id=user.id,
            title=title or original_filename,
            original_filename=original_filename,
            sha256=upload.sha256,
            file_size=upload.file_size,
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
            # DB id가 확정된 뒤 UUID object key로 같은 볼륨 안에서 atomic replace한다.
            await run_in_threadpool(storage.put_original, key, upload.staged)
        except BaseException as exc:
            # 승격 구현이나 파일시스템이 예기치 않게 실패해도 target orphan과
            # uploading 상태를 남기지 않는다. UUID key라 다른 문서를 지울 수 없다.
            try:
                await asyncio.shield(run_in_threadpool(storage.delete_original, key))
            except Exception:
                logger.warning("compensation_delete_failed", document_id=str(doc.id))
            if not isinstance(exc, Exception):
                raise
            doc.storage_key = None
            transition(doc, ProcessingStatus.FAILED)
            doc.failure_code = ErrorCode.STORAGE_UPLOAD_FAILED
            doc.failure_message = "파일 저장에 실패했습니다. 재시도해 주세요."
            await db.commit()
            if isinstance(exc, AppError):
                raise
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "파일 저장에 실패했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

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
        except BaseException as exc:
            # DB 갱신 실패 보상: 저장한 파일 제거 + 문서를 실패로 확정 (재업로드 가능하게)
            try:
                await asyncio.shield(run_in_threadpool(storage.delete_original, key))
            except Exception:
                logger.warning("compensation_delete_failed", document_id=str(doc.id))
            if not isinstance(exc, Exception):
                raise
            await db.rollback()
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
    finally:
        # promote 뒤에는 이미 경로가 없어 noop이고, 중복/DB 실패 때는 실제 파일을 지운다.
        try:
            await run_in_threadpool(storage.discard_staged_upload, upload.staged)
        except Exception:
            logger.warning("staged_upload_cleanup_failed")


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
    # pages 삭제가 blocks/lines/tables로 cascade된다)
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import text as sa_text

    from app.models.extraction import DocumentPage
    from app.models.search import DocumentChunk
    from app.models.summary import SummaryArtifact, SummaryRun

    await db.execute(sa_delete(DocumentPage).where(DocumentPage.document_id == doc.id))
    # document_chunks는 documents.id를 직접 FK로 참조하므로(soft delete 대상 밖) 별도 명시 삭제 필요
    await db.execute(sa_delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    await db.execute(
        sa_text("DELETE FROM document_chunks_fts WHERE document_id = :doc_id"),
        {"doc_id": str(doc.id)},
    )
    # 요약 run·artifact도 documents.id를 직접 FK로 참조 — 명시 삭제 필요
    await db.execute(sa_delete(SummaryArtifact).where(SummaryArtifact.document_id == doc.id))
    await db.execute(sa_delete(SummaryRun).where(SummaryRun.document_id == doc.id))
    # Q&A 스레드·메시지·claim (soft delete라 FK cascade가 안 돎 — qa 쪽 헬퍼와 공유)
    from app.services.qa.service import delete_threads_for_document

    await delete_threads_for_document(db, doc.id)
    transition(doc, ProcessingStatus.DELETED)
    doc.deleted_at = datetime.now(UTC)
    doc.storage_key = None
    doc.extraction_completed_at = None
    await db.commit()
    return doc


async def retry_document(
    db: AsyncSession, user: User, document_id: uuid.UUID, correlation_id: str
) -> Document:
    from app.services.system import runtime

    runtime.reject_if_updating()
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
        raise AppError(ErrorCode.VALIDATION_FAILED, "제목은 1~500자여야 합니다.", status_code=422)
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
