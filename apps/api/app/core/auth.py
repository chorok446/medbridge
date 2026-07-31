"""인증 공급자 계층.

비즈니스 로직은 AuthProvider 인터페이스만 알며, 공급자 교체는 이 모듈에서만 일어난다.
- LocalSingleUserProvider: 단일 사용자 로컬 설치 모드(기본). 환경 변수로 정의된
  사용자를 서버 측에서 get-or-create 한다. 클라이언트가 보낸 user_id는 절대 신뢰하지 않는다.
- SessionAuthProvider: multi_user 모드(향후 pilot/production). 세션 쿠키 기반.
"""

import uuid
from typing import Protocol

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import SESSION_COOKIE, read_session_token
from app.models.user import User


class AuthProvider(Protocol):
    async def get_current_user(self, request: Request, db: AsyncSession) -> User: ...


class LocalSingleUserProvider:
    async def get_current_user(self, request: Request, db: AsyncSession) -> User:
        settings = get_settings()
        email = settings.local_user_email.lower()
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                password_hash="!local-single-user",  # 로그인 불가 표식 (해시 아님)
                display_name=settings.local_user_display_name,
            )
            db.add(user)
            await db.commit()
        return user


class SessionAuthProvider:
    async def get_current_user(self, request: Request, db: AsyncSession) -> User:
        token = request.cookies.get(SESSION_COOKIE)
        user_id: uuid.UUID | None = read_session_token(token) if token else None
        if user_id is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "로그인이 필요합니다.", status_code=401)
        user = await db.get(User, user_id)
        if user is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "로그인이 필요합니다.", status_code=401)
        return user


def get_auth_provider() -> AuthProvider:
    if get_settings().app_mode == "single_user":
        return LocalSingleUserProvider()
    return SessionAuthProvider()
