from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# SQLite 드라이버의 암묵적 기본값에 기대지 않고, 짧은 writer 경합을 기다리는 상한을
# 명시한다. 로컬 AI 활성화는 이 대기 뒤에도 SQLITE_BUSY이면 한 번만 전체 트랜잭션을
# 재시도한다.
SQLITE_BUSY_TIMEOUT_MS = 5_000


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        if _engine.url.get_backend_name() == "sqlite":
            event.listen(_engine.sync_engine, "connect", _set_sqlite_pragmas)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def reset_engine_cache() -> None:
    """테스트에서 DATABASE_URL 변경 후 재초기화용."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
