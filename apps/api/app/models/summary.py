"""요약 실행·산출물 모델 — 모든 artifact는 하나 이상의 chunk id와 원문 출처를 가진다.

출처(source_refs_json)는 모델 출력이 아니라 선택된 chunk의 저장된 source_refs를
서버가 재조회해 구성한다. 출처 없는 artifact는 저장하지 않는다(서비스 계층에서 보장).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SummaryArtifactType, SummaryRunStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _str_enum(enum_cls: type, name: str, length: int = 30) -> Enum:
    return Enum(enum_cls, name=name, native_enum=False, length=length)


class SummaryRun(Base):
    __tablename__ = "summary_runs"
    __table_args__ = (Index("ix_summary_runs_doc_created", "document_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[SummaryRunStatus] = mapped_column(
        _str_enum(SummaryRunStatus, "summary_run_status"), default=SummaryRunStatus.QUEUED
    )
    provider_name: Mapped[str] = mapped_column(String(50))
    model_name: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    schema_version: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer)
    source_chunk_hash: Mapped[str] = mapped_column(String(64))
    learner_level: Mapped[str] = mapped_column(String(30))
    language: Mapped[str] = mapped_column(String(10), default="ko")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )


class SummarySettings(Base):
    """요약 모델 설정 — 단일 행. API 키는 여기 저장하지 않고 OS keyring에 둔다."""

    __tablename__ = "summary_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_type: Mapped[str] = mapped_column(String(30), default="disabled")
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )


class SummaryArtifact(Base):
    __tablename__ = "summary_artifacts"
    __table_args__ = (
        Index("ix_summary_artifacts_run_position", "summary_run_id", "position"),
        Index("ix_summary_artifacts_doc", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    summary_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("summary_runs.id", ondelete="CASCADE"), index=True
    )
    artifact_type: Mapped[SummaryArtifactType] = mapped_column(
        _str_enum(SummaryArtifactType, "summary_artifact_type")
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_chunk_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    source_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
