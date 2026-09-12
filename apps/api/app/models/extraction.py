"""추출 결과 모델 — 모든 텍스트는 원본 PDF 페이지·좌표(bbox)와 연결된다.

좌표 정책: PyMuPDF 페이지 좌표계(포인트 단위, 원점 좌상단, y 아래로 증가,
회전 미적용 원본 기준). 소수점 2자리로 반올림해 저장한다.
자세한 내용: docs/pdf/coordinate-system.md
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
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
from app.models.enums import BlockType, PageExtractionStatus, ScanVerdict


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _str_enum(enum_cls: type, name: str, length: int = 30) -> Enum:
    return Enum(enum_cls, name=name, native_enum=False, length=length)


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        Index("ix_document_pages_doc_page", "document_id", "page_number", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)  # 1부터 시작
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer, default=0)  # 0/90/180/270
    raw_text: Mapped[str] = mapped_column(Text, default="")
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    extraction_method: Mapped[str] = mapped_column(String(30), default="digital")
    extraction_status: Mapped[PageExtractionStatus] = mapped_column(
        _str_enum(PageExtractionStatus, "page_extraction_status"),
        default=PageExtractionStatus.PENDING,
    )
    extraction_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reading_order_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    scan_verdict: Mapped[ScanVerdict] = mapped_column(
        _str_enum(ScanVerdict, "scan_verdict"), default=ScanVerdict.UNKNOWN
    )
    text_character_count: Mapped[int] = mapped_column(Integer, default=0)
    # 현재 본문의 단어 수. OCR을 돌리면 digital + ocr 합계로 덮인다.
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    # 추출 시점의 디지털 단어 수. OCR이 덮지 않는 기준선이다 — 재실행 때 word_count는
    # 이미 합계로 바뀌어 있어 디지털 몫을 되찾을 수 없다. 예전에는 document_words를
    # COUNT(*)해서 다시 셌는데, 그 353만 행(1.27GB)의 유일한 독자가 그 쿼리였다.
    digital_word_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    image_area_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    requires_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ocr_mean_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message_internal: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (Index("ix_document_blocks_page_order", "page_id", "reading_order"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    block_index: Mapped[int] = mapped_column(Integer)  # 원본 추출 순서
    block_type: Mapped[BlockType] = mapped_column(
        _str_enum(BlockType, "block_type"), default=BlockType.TEXT
    )
    x0: Mapped[float] = mapped_column(Float)
    y0: Mapped[float] = mapped_column(Float)
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    reading_order: Mapped[int] = mapped_column(Integer, default=0)
    is_header: Mapped[bool] = mapped_column(Boolean, default=False)
    is_footer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    is_caption: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentLine(Base):
    __tablename__ = "document_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_blocks.id", ondelete="CASCADE"), index=True
    )
    line_index: Mapped[int] = mapped_column(Integer)
    x0: Mapped[float] = mapped_column(Float)
    y0: Mapped[float] = mapped_column(Float)
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    reading_order: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentTable(Base):
    __tablename__ = "document_tables"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    table_index: Mapped[int] = mapped_column(Integer)
    x0: Mapped[float] = mapped_column(Float)
    y0: Mapped[float] = mapped_column(Float)
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    cells_json: Mapped[list] = mapped_column(JSON, default=list)
    markdown_text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_status: Mapped[str] = mapped_column(String(30), default="extracted")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
