import uuid
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var
from app.db.session import get_db
from app.models.enums import ProcessingStatus
from app.models.user import User
from app.schemas.common import Envelope
from app.schemas.document import (
    DeletedOut,
    DocumentCreated,
    DocumentJobOut,
    DocumentList,
    DocumentOut,
    RenameRequest,
)
from app.services.documents import service, storage
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _decode_upload_header(value: str, *, field: str, max_length: int = 500) -> str:
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, f"잘못된 {field}입니다.", status_code=400
        ) from exc
    if not decoded or len(decoded) > max_length or any(ord(char) < 32 for char in decoded):
        raise AppError(ErrorCode.VALIDATION_FAILED, f"잘못된 {field}입니다.", status_code=400)
    return decoded


@router.post("", response_model=Envelope[DocumentCreated], status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc, duplicate = await service.create_document(db, user, file, title, correlation_id_var.get())
    return wrap(
        DocumentCreated(
            id=doc.id,
            title=doc.title,
            processing_status=doc.processing_status.value,
            processing_stage=doc.processing_stage.value,
            processing_progress=doc.processing_progress,
            duplicate=duplicate,
        )
    )


@router.post("/stream", response_model=Envelope[DocumentCreated], status_code=201)
async def upload_document_stream(
    request: Request,
    encoded_filename: str = Header(alias="X-MedBridge-Filename"),
    declared_size: int | None = Header(default=None, alias="X-MedBridge-File-Size", ge=0),
    encoded_title: str | None = Header(default=None, alias="X-MedBridge-Title"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """GUI 대형 PDF 경로: multipart spooling 없이 ASGI body를 곧바로 staging한다."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/pdf":
        raise AppError(
            ErrorCode.INVALID_FILE_TYPE, "PDF 파일만 업로드할 수 있습니다.", status_code=415
        )
    filename = _decode_upload_header(encoded_filename, field="파일 이름")
    title = (
        _decode_upload_header(encoded_title, field="문서 제목")
        if encoded_title is not None
        else None
    )
    doc, duplicate = await service.create_document_stream(
        db,
        user,
        request.stream(),
        filename,
        title,
        correlation_id_var.get(),
        declared_size=declared_size,
    )
    return wrap(
        DocumentCreated(
            id=doc.id,
            title=doc.title,
            processing_status=doc.processing_status.value,
            processing_stage=doc.processing_stage.value,
            processing_progress=doc.processing_progress,
            duplicate=duplicate,
        )
    )


@router.get("", response_model=Envelope[DocumentList])
async def list_documents(
    status: ProcessingStatus | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    docs, next_cursor = await service.list_documents(
        db, user, status=status, search=search, cursor=cursor, limit=limit
    )
    return wrap(
        DocumentList(items=[DocumentOut.model_validate(d) for d in docs], next_cursor=next_cursor)
    )


@router.get("/{document_id}", response_model=Envelope[DocumentOut])
async def get_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await service.get_owned_document(db, user, document_id)
    return wrap(DocumentOut.model_validate(doc))


@router.get("/{document_id}/file")
async def get_document_file(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """PDF 원문 반환 (미리보기용). 소유권 검사 후 로컬 파일을 스트리밍한다."""
    doc = await service.get_owned_document(db, user, document_id)
    if doc.storage_key is None:
        raise AppError(ErrorCode.NOT_FOUND, "원본 파일이 없습니다.", status_code=404)
    path = storage.get_storage().resolve_path(doc.storage_key)
    if not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "원본 파일이 없습니다.", status_code=404)
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")


@router.delete("/{document_id}", response_model=Envelope[DeletedOut])
async def delete_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await service.delete_document(db, user, document_id)
    return wrap(DeletedOut(id=doc.id, processing_status=doc.processing_status.value))


@router.post("/{document_id}/retry", response_model=Envelope[DocumentOut])
async def retry_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await service.retry_document(db, user, document_id, correlation_id_var.get())
    return wrap(DocumentOut.model_validate(doc))


@router.patch("/{document_id}", response_model=Envelope[DocumentOut])
async def rename_document(
    document_id: uuid.UUID,
    body: RenameRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await service.rename_document(db, user, document_id, body.title)
    return wrap(DocumentOut.model_validate(doc))


@router.get("/{document_id}/jobs", response_model=Envelope[list[DocumentJobOut]])
async def list_jobs(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    jobs = await service.list_jobs(db, user, document_id)
    return wrap([DocumentJobOut.model_validate(j) for j in jobs])
