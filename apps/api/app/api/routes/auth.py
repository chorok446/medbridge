from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, UserOut
from app.schemas.common import Envelope
from app.utils.responses import wrap

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_multi_user() -> None:
    """단일 사용자 로컬 모드에서는 공개 회원가입·로그인을 제공하지 않는다."""
    if get_settings().app_mode == "single_user":
        raise AppError(ErrorCode.NOT_FOUND, "요청한 리소스를 찾을 수 없습니다.", status_code=404)


def _set_session(response: Response, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id),
        max_age=get_settings().session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=get_settings().app_env == "production",
        path="/",
    )


@router.post("/register", response_model=Envelope[UserOut], status_code=201)
async def register(
    body: RegisterRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_multi_user()
    exists = (
        await db.execute(select(User.id).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if exists is not None:
        raise AppError(ErrorCode.EMAIL_TAKEN, "이미 가입된 이메일입니다.", status_code=409)
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    await db.commit()
    _set_session(response, user)
    return wrap(UserOut.model_validate(user))


@router.post("/login", response_model=Envelope[UserOut])
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    _require_multi_user()
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, body.password):
        raise AppError(
            ErrorCode.INVALID_CREDENTIALS,
            "이메일 또는 비밀번호가 올바르지 않습니다.",
            status_code=401,
        )
    _set_session(response, user)
    return wrap(UserOut.model_validate(user))


@router.post("/logout", status_code=204)
async def logout(response: Response) -> Response:
    _require_multi_user()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=Envelope[UserOut])
async def me(user: User = Depends(get_current_user)) -> dict:
    return wrap(UserOut.model_validate(user))
