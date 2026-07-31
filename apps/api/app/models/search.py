"""검색 청크 모델 — 모든 청크는 하나 이상의 원문 위치(source_refs_json)를 가진다.

source_refs_json 항목: {pageNumber, blockId, bbox:[x0,y0,x1,y1], readingOrder, sourceMethod}.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_doc_order", "document_id", "chunk_index"),
        Index("ix_document_chunks_doc_hash", "document_id", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    source_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
