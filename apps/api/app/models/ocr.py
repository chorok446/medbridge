"""OCR 실행 기록 — 페이지 단위 실행의 엔진·설정·품질 지표를 남긴다."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import OcrRunStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OcrRun(Base):
    __tablename__ = "ocr_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(30), default="tesseract")
    engine_version: Mapped[str] = mapped_column(String(30), default="")
    language: Mapped[str] = mapped_column(String(30), default="kor+eng")
    render_dpi: Mapped[int] = mapped_column(Integer, default=300)
    preprocessing_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[OcrRunStatus] = mapped_column(
        Enum(OcrRunStatus, name="ocr_run_status", native_enum=False, length=30),
        default=OcrRunStatus.RUNNING,
    )
    mean_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    median_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    low_confidence_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
