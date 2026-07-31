import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    DocumentType,
    JobStatus,
    JobType,
    ProcessingStage,
    ProcessingStatus,
)


def _str_enum(enum_cls: type, name: str, length: int = 30) -> Enum:
    # SQLite 호환: 네이티브 enum 대신 VARCHAR 저장 (값 목록은 코드 enum이 단일 원천)
    return Enum(enum_cls, name=name, native_enum=False, length=length)


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_user_created", "user_id", "created_at"),
        Index("ix_documents_user_sha256", "user_id", "sha256"),
        Index("ix_documents_status", "processing_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_profile.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    redacted_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    file_size: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_type: Mapped[DocumentType] = mapped_column(
        _str_enum(DocumentType, "document_type"), default=DocumentType.UNKNOWN
    )
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        _str_enum(ProcessingStatus, "processing_status"), default=ProcessingStatus.CREATED
    )
    processing_stage: Mapped[ProcessingStage] = mapped_column(
        _str_enum(ProcessingStage, "processing_stage"), default=ProcessingStage.UPLOAD
    )
    processing_progress: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_evidence_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 콘텐츠 revision — 추출/OCR 완료로 내용이 바뀔 때마다 +1. 청크/요약 stale 판정 기준.
    content_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 현재 청크 세트가 만들어진 시점의 content_revision (다르면 청크 stale). NULL=아직 없음.
    chunk_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 추출 캐시·재처리 판단용 (docs §18)
    extraction_engine: Mapped[str | None] = mapped_column(String(30), nullable=True)
    extraction_engine_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    extraction_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentJob(Base):
    __tablename__ = "document_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[JobType] = mapped_column(_str_enum(JobType, "job_type"))
    status: Mapped[JobStatus] = mapped_column(
        _str_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    correlation_id: Mapped[str] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
