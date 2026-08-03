"""문서 기반 Q&A 모델 — 스레드·메시지·주장(claim).

한 스레드는 한 문서에만 속한다. 각 claim은 답변 생성 당시 검증된 출처(page/bbox 등)를
스냅샷으로 보관해, 청크가 재생성돼 UUID가 사라져도 과거 답변의 출처를 표시할 수 있다.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
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
from app.models.enums import QaClaimVerification, QaMessageRole, QaMessageStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _str_enum(enum_cls: type, name: str, length: int = 30) -> Enum:
    # values_callable로 enum의 .value(소문자)를 저장한다 — 기본값은 멤버 NAME(대문자)을
    # 저장해, 부분 유니크 인덱스의 WHERE role='assistant' AND status='pending'(소문자
    # 리터럴)와 어긋나 인덱스가 무력화된다.
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class QaThread(Base):
    __tablename__ = "qa_threads"
    __table_args__ = (Index("ix_qa_threads_doc_updated", "document_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_profile.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QaMessage(Base):
    __tablename__ = "qa_messages"
    __table_args__ = (
        Index("uq_qa_messages_thread_seq", "thread_id", "sequence_number", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[QaMessageRole] = mapped_column(_str_enum(QaMessageRole, "qa_message_role"))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[QaMessageStatus] = mapped_column(
        _str_enum(QaMessageStatus, "qa_message_status"), default=QaMessageStatus.COMPLETED
    )
    sequence_number: Mapped[int] = mapped_column(Integer)
    document_revision: Mapped[int] = mapped_column(Integer, default=0)
    chunk_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retrieval_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 모델이 제안한 다음 질문. 없으면 NULL — 기존 메시지는 백필하지 않는다.
    followups_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 스트리밍(Sprint 4B) — 전부 nullable, 기존 행 무해
    draft_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 부분 유니크 인덱스(uq_qa_stream_request_id)로 유일성 보장 — 컬럼 unique는 두지 않는다
    stream_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stream_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stream_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stream_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 재시도 원본 메시지 참조 — SQLite ALTER는 FK 추가를 못하므로 앱 레벨 참조(plain Uuid)
    retry_of_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QaClaim(Base):
    __tablename__ = "qa_claims"
    __table_args__ = (
        Index("uq_qa_claims_message_index", "message_id", "claim_index", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_messages.id", ondelete="CASCADE"), index=True
    )
    claim_index: Mapped[int] = mapped_column(Integer)
    claim_text: Mapped[str] = mapped_column(Text, default="")
    verification_status: Mapped[QaClaimVerification] = mapped_column(
        _str_enum(QaClaimVerification, "qa_claim_verification")
    )
    source_chunk_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    source_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
