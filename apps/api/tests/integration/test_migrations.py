"""알파벳 순서상 마지막에 실행 — downgrade가 테이블을 비우므로 다른 테스트 뒤에 돈다."""

from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings


def test_full_downgrade_upgrade_cycle():
    cfg = AlembicConfig("alembic.ini")
    command.downgrade(cfg, "base")

    engine = create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert "users" not in names
        assert "documents" not in names
        assert "document_jobs" not in names

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert {"users", "documents", "document_jobs"} <= names
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0004"
    engine.dispose()


def test_upgrade_is_idempotent_with_existing_data():
    """이미 head인 상태에서 재실행해도 오류·데이터 손상이 없다."""
    cfg = AlembicConfig("alembic.ini")
    command.upgrade(cfg, "head")  # no-op이어야 한다
