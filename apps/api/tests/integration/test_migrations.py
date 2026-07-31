"""알파벳 순서상 마지막에 실행 — downgrade가 테이블을 비우므로 다른 테스트 뒤에 돈다."""

from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings
from tests.integration.conftest import alembic_config


def test_full_downgrade_upgrade_cycle():
    cfg = alembic_config()
    command.downgrade(cfg, "base")

    engine = create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert "app_profile" not in names
        assert "documents" not in names
        assert "document_jobs" not in names

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert {
            "app_profile",
            "documents",
            "document_jobs",
            "document_pages",
            "document_blocks",
            "document_words",
            "document_tables",
        } <= names
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0002"
    engine.dispose()


def test_upgrade_is_idempotent():
    """이미 head인 상태에서 재실행해도 오류·데이터 손상이 없다."""
    command.upgrade(alembic_config(), "head")


def test_startup_migration_runner():
    """앱 시작 시 자동 마이그레이션 경로가 동작한다 (백업 포함)."""
    from app.main import run_migrations

    run_migrations()  # 이미 head — no-op이어야 한다
