"""통합 테스트 — 외부 인프라 없이(PostgreSQL·Redis·MinIO 불필요) 실행된다.

SQLite DB와 문서 파일은 테스트 전용 임시 앱 데이터 디렉터리에 격리되고,
작업은 LocalTaskRunner가 같은 이벤트 루프에서 실행한다.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alembic import command
from app.core.paths import get_path_provider
from app.db.session import get_session_factory
from app.main import app
from app.services.tasks.runner import get_task_runner


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("script_location", str(Path("alembic").resolve()))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def prepare_infra():
    get_path_provider().ensure_directories()
    # 빈 SQLite DB에서 alembic upgrade head 성공 (요구사항 검증을 겸한다)
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(autouse=True)
async def clean_db(prepare_infra):
    yield
    await get_task_runner().drain()
    async with get_session_factory()() as session:
        for table in ("document_jobs", "documents", "app_profile"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def drain_jobs() -> None:
    """업로드가 자동 등록한 검증 작업 완료 대기."""
    await get_task_runner().drain()
