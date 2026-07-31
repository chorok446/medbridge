import uuid
from datetime import datetime

from app.schemas.common import CamelModel


class DocumentOut(CamelModel):
    id: uuid.UUID
    title: str
    original_filename: str
    file_size: int
    page_count: int | None
    document_type: str
    processing_status: str
    processing_stage: str
    processing_progress: int
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentCreated(CamelModel):
    id: uuid.UUID
    title: str
    processing_status: str
    processing_stage: str
    processing_progress: int
    duplicate: bool


class DocumentList(CamelModel):
    items: list[DocumentOut]
    next_cursor: str | None


class DocumentJobOut(CamelModel):
    id: uuid.UUID
    job_type: str
    status: str
    attempt_count: int
    max_attempts: int
    correlation_id: str
    failure_code: str | None
    failure_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class RenameRequest(CamelModel):
    title: str


class DownloadUrl(CamelModel):
    url: str
    expires_in_seconds: int


class DeletedOut(CamelModel):
    id: uuid.UUID
    processing_status: str
