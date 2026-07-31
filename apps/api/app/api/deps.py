from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_or_create_profile
from app.db.session import get_db
from app.models.user import User


async def get_current_user(db: AsyncSession = Depends(get_db)) -> User:
    """현재 사용자(로컬 프로필) 주입의 단일 통로."""
    return await get_or_create_profile(db)
