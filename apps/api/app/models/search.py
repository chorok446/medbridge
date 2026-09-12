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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index(
            "ix_document_chunks_doc_generation_order",
            "document_id",
            "generation_id",
            "chunk_index",
        ),
        Index(
            "ix_document_chunks_doc_generation_hash",
            "document_id",
            "generation_id",
            "content_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # NULL은 0014 이전 legacy 활성 세트. 새 청크는 generation별로 같은 테이블에
    # shadow 저장되고 Document.active_chunk_generation_id와 일치할 때만 조회된다.
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_chunk_generations.id", ondelete="CASCADE"),
        nullable=True,
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


class DocumentChunkGeneration(Base):
    """활성 청크를 건드리지 않고 새 청크 세트를 만드는 shadow generation.

    ``source_revision``은 계획을 세운 시점의 문서 revision이다. staging이 여러 번
    commit되더라도 활성 전환 직전에 이 값을 다시 검사해, 중간에 OCR/추출 결과가
    바뀐 generation은 절대 활성화하지 않는다.
    """

    __tablename__ = "document_chunk_generations"
    __table_args__ = (
        Index("ix_chunk_generations_document_created", "document_id", "created_at"),
        Index(
            "uq_chunk_generations_building_document",
            "document_id",
            unique=True,
            sqlite_where=text("status = 'building'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    source_revision: Mapped[int] = mapped_column(Integer)
    shadow_document_id: Mapped[str] = mapped_column(String(50), unique=True)
    owner_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="building", server_default="building")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
