"""downgrade가 테이블을 비우므로 실패해도 반드시 head로 복구한다 — DB는 세션 전체가 공유한다."""

import logging
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings
from tests.integration.conftest import alembic_config, head_revision


def test_full_downgrade_upgrade_cycle():
    cfg = alembic_config()
    command.downgrade(cfg, "base")

    engine = create_engine(get_settings().database_url_sync)
    try:
        try:
            with engine.connect() as conn:
                names = set(inspect(conn).get_table_names())
                assert "app_profile" not in names
                assert "documents" not in names
                assert "document_jobs" not in names
        finally:
            # 단언이 실패해도 head로 되돌린다 — 안 그러면 뒤에 도는 통합 테스트 전부가
            # 테이블 부재로 연쇄 실패해 실제 원인이 묻힌다.
            command.upgrade(cfg, "head")

        with engine.connect() as conn:
            names = set(inspect(conn).get_table_names())
            assert {
                "app_profile",
                "documents",
                "document_jobs",
                "document_pages",
                "document_blocks",
                "document_tables",
                "document_chunks",
                "document_chunks_fts",
                "summary_runs",
                "summary_artifacts",
                "summary_nodes",
                "qa_threads",
                "qa_messages",
                "qa_claims",
            } <= names
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == head_revision()
    finally:
        engine.dispose()


def test_word_table_is_gone_and_its_replacement_is_present():
    """단어 테이블을 없애고 그 유일한 독자(디지털 단어 수)를 페이지에 남겼는지 확인한다.

    353만 행·1.27GB가 COUNT(*) 하나 때문에 남아 있었다. 컬럼을 빠뜨린 채 테이블만
    지우면 OCR 재실행이 디지털 단어를 0으로 보고 word_count를 낮춰 잡는다.
    """
    engine = create_engine(get_settings().database_url_sync)
    with engine.connect() as conn:
        assert "document_words" not in set(inspect(conn).get_table_names())
        columns = {c["name"] for c in inspect(conn).get_columns("document_pages")}
        assert "digital_word_count" in columns
    engine.dispose()


def test_upgrade_is_idempotent():
    """이미 head인 상태에서 재실행해도 오류·데이터 손상이 없다."""
    command.upgrade(alembic_config(), "head")


def test_sqlite_online_backup_includes_committed_wal_rows(tmp_path):
    from app.main import _backup_sqlite_database

    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    with closing(sqlite3.connect(str(source_path))) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        writer.commit()
        writer.execute("INSERT INTO probe VALUES ('committed-in-wal')")
        writer.commit()

        wal_path = Path(f"{source_path}-wal")
        assert wal_path.is_file() and wal_path.stat().st_size > 0
        _backup_sqlite_database(source_path, backup_path)

    with closing(sqlite3.connect(str(backup_path))) as restored:
        assert restored.execute("SELECT value FROM probe").fetchall() == [
            ("committed-in-wal",)
        ]
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with pytest.raises(FileExistsError):
        _backup_sqlite_database(source_path, backup_path)


def test_startup_migration_runner():
    """앱 시작 시 자동 마이그레이션 경로가 동작한다 (백업 포함)."""
    from app.main import run_migrations

    run_migrations()  # 이미 head — no-op이어야 한다


def test_startup_migration_preserves_structured_sidecar_file_logging():
    """embedded Alembic이 앱 root FileHandler를 제거하지 않고 structlog도 그 경로를 쓴다."""
    from app.core.logging import configure_logging, get_logger
    from app.core.paths import get_path_provider
    from app.main import run_migrations

    configure_logging()
    root = logging.getLogger()
    before = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]
    assert len(before) == 1

    run_migrations()
    sentinel = f"migration-log-sentinel-{uuid.uuid4()}"
    get_logger("tests.migration_logging").warning(
        "migration_logging_probe", marker=sentinel
    )
    for handler in root.handlers:
        handler.flush()

    after = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]
    assert after == before
    log_file = get_path_provider().logs_dir / "sidecar.log"
    logged = log_file.read_text(encoding="utf-8", errors="replace")
    assert "migration_logging_probe" in logged
    assert sentinel in logged
