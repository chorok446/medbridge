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
    Text,
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
    # 계층 요약 진행률 — 계획된 노드 수와 완료(재사용 포함) 노드 수. 0이면 아직 계획 전.
    planned_nodes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_nodes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 같은 error_code 안에서 어느 계약이 깨졌는지 가리키는 분류값(원문 없음).
    # 사용자에게 보여주지 않는다 — 오류 보고서 진단용이다.
    failure_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )


# 설정 행의 고정 id. "단일 행"을 주석이 아니라 PK로 강제한다 — 임의 UUID를 쓰면
# 설정 저장과 로컬 AI 활성화가 각각 'SELECT → 없으면 INSERT'를 하다 겹칠 때 행이 조용히
# 2개가 되고, 그 순간부터 요약·질문·설정이 전부 500이 된다. 같은 id면 두 번째 INSERT가
# PK 위반으로 즉시 실패하므로 중복이 만들어지지 않는다.
SETTINGS_SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-00005e771495")


class SummarySettings(Base):
    """요약 모델 설정 — 단일 행. API 키는 여기 저장하지 않고 OS keyring에 둔다."""

    __tablename__ = "summary_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=lambda: SETTINGS_SINGLETON_ID
    )
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


class SummaryNode(Base):
    """계층 요약 체크포인트 — 한 번 성공한 중간 요약을 재사용·재개 단위로 보존한다.

    출처(source_chunk_ids_json)는 모델 출력이 아니라 서버가 계산한 값이다. 레벨 0은
    그룹에 포함된 chunk id, 레벨 1+는 자식 노드 출처의 합집합이다.
    """

    __tablename__ = "summary_nodes"
    __table_args__ = (
        # 한 run 안에서 (레벨, 위치)는 유일하다 — 중복 실행·중복 저장 방지
        Index("uq_summary_nodes_run_level_pos", "summary_run_id", "level", "position", unique=True),
        # 재사용 조회: 같은 문서에서 같은 input_hash를 가진 성공 노드를 찾는다
        Index("ix_summary_nodes_doc_hash", "document_id", "input_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    summary_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("summary_runs.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    source_chunk_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
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
