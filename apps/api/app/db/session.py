from collections.abc import AsyncIterator

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, with_loader_criteria

from app.core.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# SQLite 드라이버의 암묵적 기본값에 기대지 않고, 짧은 writer 경합을 기다리는 상한을
# 명시한다. 로컬 AI 활성화는 이 대기 뒤에도 SQLITE_BUSY이면 한 번만 전체 트랜잭션을
# 재시도한다.
SQLITE_BUSY_TIMEOUT_MS = 5_000


@event.listens_for(Session, "do_orm_execute")
def _only_active_document_chunks(execute_state) -> None:
    """모든 ORM 청크 조회를 문서의 활성 generation으로 한정한다.

    generation 작성/정리 코드는 ``include_inactive_chunks`` 실행 옵션으로 명시적으로
    우회한다. 이 중앙 필터 덕분에 검색·요약 등 기존 소비자는 완성 중인 shadow 행을
    볼 수 없고, 활성 포인터 UPDATE 한 건으로 새 세트를 원자적으로 보게 된다.
    """
    if not execute_state.is_select or execute_state.execution_options.get(
        "include_inactive_chunks", False
    ):
        return
    from app.models.document import Document
    from app.models.search import DocumentChunk

    active_generation = (
        select(Document.active_chunk_generation_id)
        .where(Document.id == DocumentChunk.document_id)
        .scalar_subquery()
    )
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            DocumentChunk,
            lambda chunk: chunk.generation_id.is_(active_generation),
            include_aliases=True,
        )
    )


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
