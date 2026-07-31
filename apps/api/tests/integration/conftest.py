"""통합 테스트 — 로컬 Docker Compose의 postgres/redis/minio가 떠 있어야 한다.

각 테스트는 실제 PostgreSQL(medbridge_test DB), MinIO 테스트 버킷, Redis를 사용한다.
worker 검증은 dramatiq actor를 인라인 호출로 실행한다 (브로커 경유 전 구간은
scripts/upload-sample.sh 로 검증).
"""

from collections.abc import AsyncIterator

import psycopg
import pytest
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import app
from app.services.documents.storage import internal_client

ADMIN_DSN = "postgresql://medbridge:medbridge@localhost:5432/postgres"


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig("alembic.ini")
    return cfg


@pytest.fixture(scope="session", autouse=True)
def prepare_infra():
    # 1) 빈 테스트 DB 준비
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'medbridge_test'"
        ).fetchone()
        if exists:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'medbridge_test' AND pid <> pg_backend_pid()"
            )
            conn.execute("DROP DATABASE medbridge_test")
        conn.execute("CREATE DATABASE medbridge_test")

    # 2) 빈 DB에서 alembic upgrade head 성공 (요구사항 검증을 겸한다)
    command.upgrade(_alembic_config(), "head")

    # 3) MinIO 테스트 버킷
    client = internal_client()
    bucket = get_settings().minio_bucket_originals
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        client.create_bucket(Bucket=bucket)
    yield


@pytest.fixture(autouse=True)
async def clean_db(prepare_infra):
    yield
    async with get_session_factory()() as session:
        await session.execute(text("TRUNCATE document_jobs, documents, users CASCADE"))
        await session.commit()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
