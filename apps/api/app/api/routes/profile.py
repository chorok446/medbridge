"""앱 설정(로컬 프로필) 조회·수정. 계정 개념 없이 표시 이름·학습 수준만 다룬다."""

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.enums import StudyLevel
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.utils.responses import wrap

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileOut(CamelModel):
    display_name: str
    study_level: int
    preferred_language: str
    external_ai_allowed: bool


class ProfileUpdate(CamelModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    study_level: int | None = Field(default=None, ge=0, le=3)
    preferred_language: str | None = Field(default=None, max_length=10)
    external_ai_allowed: bool | None = None


@router.get("", response_model=Envelope[ProfileOut])
async def get_profile(user: User = Depends(get_current_user)) -> dict:
    return wrap(ProfileOut.model_validate(user))


@router.patch("", response_model=Envelope[ProfileOut])
async def update_profile(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.study_level is not None:
        user.study_level = StudyLevel(body.study_level)
    if body.preferred_language is not None:
        user.preferred_language = body.preferred_language
    if body.external_ai_allowed is not None:
        user.external_ai_allowed = body.external_ai_allowed
    await db.commit()
    await db.refresh(user)
    return wrap(ProfileOut.model_validate(user))
