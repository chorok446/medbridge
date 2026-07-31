"""단일 사용자 로컬 프로필 주입.

데스크톱 앱에는 로그인이 없다. 모든 요청은 app_profile의 유일한 행(로컬 프로필)에
귀속되며, 클라이언트가 보낸 user_id는 절대 신뢰하지 않는다.
향후 서버 모드가 필요해지면 이 지점(get_current_user 의존성)만 교체하면 된다.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_or_create_profile(db: AsyncSession) -> User:
    profile = (await db.execute(select(User).limit(1))).scalar_one_or_none()
    if profile is None:
        profile = User()
        db.add(profile)
        await db.commit()
    return profile
