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


def head_revision() -> str:
    """스크립트 디렉터리 기준 head — 마이그레이션이 늘 때마다 테스트의 하드코딩이
    깨지지 않게 한 곳에서 계산한다."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head() or ""


@pytest.fixture(scope="session", autouse=True)
def prepare_infra():
    get_path_provider().ensure_directories()
    # API 키가 실제 OS credential storage를 건드리지 않도록 in-memory keyring 주입
    from app.services.summary import secrets

    secrets.use_in_memory_backend()
    # 빈 SQLite DB에서 alembic upgrade head 성공 (요구사항 검증을 겸한다)
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(autouse=True)
async def clean_db(prepare_infra):
    yield
    await get_task_runner().drain()
    async with get_session_factory()() as session:
        # summary_settings는 문서와 무관한 단일 행이라 명시적으로 비운다
        for table in (
            "qa_claims",
            "qa_messages",
            "qa_threads",
            "summary_artifacts",
            "summary_runs",
            "document_chunks",
            "document_jobs",
            "documents",
            "summary_settings",
            "app_profile",
        ):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text("DELETE FROM document_chunks_fts"))
        await session.commit()
    # keyring도 테스트 간 격리
    from app.services.summary import secrets

    secrets.delete_api_key()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def drain_jobs() -> None:
    """업로드가 자동 등록한 검증 작업 완료 대기."""
    await get_task_runner().drain()
