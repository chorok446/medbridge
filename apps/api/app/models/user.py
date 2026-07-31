import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import StudyLevel


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class User(Base):
    """단일 사용자 로컬 프로필 (app_profile 테이블).

    데스크톱 앱에는 계정이 없다 — 이 테이블은 항상 1행이며 문서 소유권(user_id FK)의
    내부 기준점으로만 쓰인다. GUI에 ID·이메일 같은 계정 개념을 노출하지 않는다.
    """

    __tablename__ = "app_profile"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(100), default="MedBridge 사용자")
    study_level: Mapped[StudyLevel] = mapped_column(
        Enum(StudyLevel, name="study_level", native_enum=False, length=20),
        default=StudyLevel.BASIC,
    )
    preferred_language: Mapped[str] = mapped_column(String(10), default="ko")
    external_ai_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )
