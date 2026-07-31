import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
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
    DownloadUrl,
    RenameRequest,
)
from app.services.documents import service
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["documents"])


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


@router.get("/{document_id}/download-url", response_model=Envelope[DownloadUrl])
async def download_url(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    url = await service.download_url(db, user, document_id)
    return wrap(DownloadUrl(url=url, expires_in_seconds=get_settings().presign_expiry_seconds))
