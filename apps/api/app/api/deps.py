from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_auth_provider
from app.db.session import get_db
from app.models.user import User


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """현재 사용자 주입의 단일 통로. 공급자 선택은 core.auth에서만 한다."""
    return await get_auth_provider().get_current_user(request, db)
