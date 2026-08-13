"""downgrade가 테이블을 비우므로 실패해도 반드시 head로 복구한다 — DB는 세션 전체가 공유한다."""

import logging
import os
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


def test_0013_preserves_existing_page_counts_and_database_integrity(
    tmp_path, monkeypatch
):
    """0012 실데이터가 있는 DB를 0013으로 올려 디지털 단어 수와 FK를 검증한다.

    빈 DB의 head upgrade만으로는 0013의 상관 서브쿼리 백필이나 대용량 사용자 DB에서
    실제로 존재하는 OCR/digital 혼합 데이터를 전혀 실행하지 못한다. 운영 DB를 저장소에
    넣을 수는 없으므로, 기존 문서·두 페이지·혼합 source_method를 갖는 최소 사본으로
    파괴적 DROP 직전/직후 계약을 고정한다.
    """
    database = tmp_path / "existing-0012.db"
    original_url = get_settings().database_url_sync
    original_database_env = os.environ.get("DATABASE_URL")

    try:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        cfg = alembic_config()
        command.upgrade(cfg, "0012")

        user_id = uuid.uuid4().hex
        document_id = uuid.uuid4().hex
        first_page_id = uuid.uuid4().hex
        second_page_id = uuid.uuid4().hex
        with closing(sqlite3.connect(str(database))) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO app_profile (id) VALUES (?)", (user_id,))
            conn.execute(
                """
                INSERT INTO documents (
                    id, user_id, title, original_filename, sha256, file_size
                ) VALUES (?, ?, '대형 문서', 'large.pdf', ?, 314572800)
                """,
                (document_id, user_id, "a" * 64),
            )
            for page_id, page_number, word_count in (
                (first_page_id, 1, 4),
                (second_page_id, 2, 2),
            ):
                conn.execute(
                    """
                    INSERT INTO document_pages (
                        id, document_id, page_number, width, height, word_count
                    ) VALUES (?, ?, ?, 612, 792, ?)
                    """,
                    (page_id, document_id, page_number, word_count),
                )
            word_sql = """
                INSERT INTO document_words (
                    id, page_id, word_index, x0, y0, x1, y1, text,
                    normalized_text, confidence, metadata_json, source_method
                ) VALUES (?, ?, ?, 0, 0, 10, 10, ?, ?, 1.0, '{}', ?)
            """
            rows = (
                (uuid.uuid4().hex, first_page_id, 0, "digital-1", "digital-1", "digital"),
                (uuid.uuid4().hex, first_page_id, 1, "digital-2", "digital-2", "digital"),
                (uuid.uuid4().hex, first_page_id, 2, "digital-3", "digital-3", "digital"),
                (uuid.uuid4().hex, first_page_id, 3, "ocr", "ocr", "ocr"),
                (uuid.uuid4().hex, second_page_id, 0, "ocr-1", "ocr-1", "ocr"),
                (uuid.uuid4().hex, second_page_id, 1, "ocr-2", "ocr-2", "ocr"),
            )
            conn.executemany(word_sql, rows)
            conn.commit()

        command.upgrade(cfg, "0013")

        with closing(sqlite3.connect(str(database))) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            assert "document_words" not in tables
            assert conn.execute(
                """
                SELECT page_number, digital_word_count, word_count
                  FROM document_pages
                 ORDER BY page_number
                """
            ).fetchall() == [(1, 3, 4), (2, 0, 2)]
            assert conn.execute(
                "SELECT title, file_size FROM documents WHERE id = ?", (document_id,)
            ).fetchone() == ("대형 문서", 314572800)
            assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        # 테스트 전역 Settings가 임시 DB URL을 계속 가리키면 뒤 테스트가 공유 DB 대신
        # 삭제될 tmp_path를 열게 된다. 환경 복원 후 원래 URL이 돌아왔는지도 확인한다.
        if original_database_env is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", original_database_env)
        get_settings.cache_clear()
        assert get_settings().database_url_sync == original_url


def test_upgrade_is_idempotent():
    """이미 head인 상태에서 재실행해도 오류·데이터 손상이 없다."""
    command.upgrade(alembic_config(), "head")


def test_0014_preserves_legacy_chunks_and_replaces_indexes(tmp_path, monkeypatch):
    """legacy NULL generation을 그대로 활성 상태로 두고 인덱스만 generation-aware로 바꾼다."""
    database = tmp_path / "existing-0013-chunks.db"
    original_url = get_settings().database_url_sync
    original_database_env = os.environ.get("DATABASE_URL")

    try:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        cfg = alembic_config()
        command.upgrade(cfg, "0013")

        user_id = uuid.uuid4().hex
        document_id = uuid.uuid4().hex
        chunk_id = uuid.uuid4().hex
        with closing(sqlite3.connect(str(database))) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO app_profile (id) VALUES (?)", (user_id,))
            conn.execute(
                """
                INSERT INTO documents (
                    id, user_id, title, original_filename, sha256, file_size,
                    content_revision, chunk_revision
                ) VALUES (?, ?, 'legacy', 'legacy.pdf', ?, 1024, 7, 7)
                """,
                (document_id, user_id, "a" * 64),
            )
            conn.execute(
                """
                INSERT INTO document_chunks (
                    id, document_id, chunk_index, normalized_text, token_count,
                    page_start, page_end, source_refs_json, content_hash
                ) VALUES (?, ?, 0, '보존할 청크', 3, 1, 1, '[]', ?)
                """,
                (chunk_id, document_id, "b" * 64),
            )
            conn.execute(
                """
                INSERT INTO document_chunks_fts (
                    chunk_id, document_id, normalized_text, section_title
                ) VALUES (?, ?, '보존할 청크', '')
                """,
                (chunk_id, str(uuid.UUID(document_id))),
            )
            conn.commit()

        command.upgrade(cfg, "0014")

        with closing(sqlite3.connect(str(database))) as conn:
            assert conn.execute(
                "SELECT active_chunk_generation_id FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone() == (None,)
            assert conn.execute(
                "SELECT normalized_text, generation_id FROM document_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone() == ("보존할 청크", None)
            assert conn.execute(
                "SELECT normalized_text FROM document_chunks_fts WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone() == ("보존할 청크",)
            indexes = {row[1] for row in conn.execute("PRAGMA index_list('document_chunks')")}
            assert "ix_document_chunks_doc_generation_order" in indexes
            assert "ix_document_chunks_doc_generation_hash" in indexes
            assert "ix_document_chunks_doc_order" not in indexes
            assert "ix_document_chunks_doc_hash" not in indexes
            plan = "\n".join(
                row[3]
                for row in conn.execute(
                    """
                    EXPLAIN QUERY PLAN SELECT id FROM document_chunks
                     WHERE document_id = ? AND generation_id IS NULL
                     ORDER BY chunk_index
                    """,
                    (document_id,),
                )
            )
            assert "ix_document_chunks_doc_generation_order" in plan
            assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        if original_database_env is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", original_database_env)
        get_settings.cache_clear()
        assert get_settings().database_url_sync == original_url


def test_0014_downgrade_promotes_only_active_generation(tmp_path, monkeypatch):
    """0013 rollback 뒤에는 active 청크/FTS만 legacy key로 남아 검색 가능해야 한다."""
    database = tmp_path / "active-0014-downgrade.db"
    original_url = get_settings().database_url_sync
    original_database_env = os.environ.get("DATABASE_URL")

    try:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        cfg = alembic_config()
        command.upgrade(cfg, "0014")

        user_id = uuid.uuid4().hex
        document_id = uuid.uuid4().hex
        active_generation = uuid.uuid4().hex
        inactive_generation = uuid.uuid4().hex
        active_chunk = uuid.uuid4().hex
        inactive_chunk = uuid.uuid4().hex
        legacy_chunk = uuid.uuid4().hex
        active_key = f"shadow:{uuid.UUID(active_generation)}"
        inactive_key = f"shadow:{uuid.UUID(inactive_generation)}"
        with closing(sqlite3.connect(str(database))) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO app_profile (id) VALUES (?)", (user_id,))
            conn.execute(
                """
                INSERT INTO documents (
                    id, user_id, title, original_filename, sha256, file_size,
                    content_revision, chunk_revision, active_chunk_generation_id
                ) VALUES (?, ?, 'generation', 'generation.pdf', ?, 1024, 3, 3, ?)
                """,
                (document_id, user_id, "a" * 64, active_generation),
            )
            generation_sql = """
                INSERT INTO document_chunk_generations (
                    id, document_id, source_revision, shadow_document_id, status
                ) VALUES (?, ?, 3, ?, ?)
            """
            conn.execute(
                generation_sql,
                (active_generation, document_id, active_key, "active"),
            )
            conn.execute(
                generation_sql,
                (inactive_generation, document_id, inactive_key, "old"),
            )
            chunk_sql = """
                INSERT INTO document_chunks (
                    id, document_id, generation_id, chunk_index, normalized_text,
                    token_count, page_start, page_end, source_refs_json, content_hash
                ) VALUES (?, ?, ?, 0, ?, 3, 1, 1, '[]', ?)
            """
            conn.execute(
                chunk_sql,
                (active_chunk, document_id, active_generation, "활성 청크", "b" * 64),
            )
            conn.execute(
                chunk_sql,
                (inactive_chunk, document_id, inactive_generation, "이전 청크", "c" * 64),
            )
            conn.execute(
                chunk_sql,
                (legacy_chunk, document_id, None, "전환 전 legacy 청크", "d" * 64),
            )
            fts_sql = """
                INSERT INTO document_chunks_fts (
                    chunk_id, document_id, normalized_text, section_title
                ) VALUES (?, ?, ?, '')
            """
            conn.execute(fts_sql, (active_chunk, active_key, "활성 청크"))
            conn.execute(fts_sql, (inactive_chunk, inactive_key, "이전 청크"))
            conn.execute(
                fts_sql,
                (legacy_chunk, str(uuid.UUID(document_id)), "전환 전 legacy 청크"),
            )
            conn.commit()

        command.downgrade(cfg, "0013")

        with closing(sqlite3.connect(str(database))) as conn:
            assert conn.execute(
                "SELECT id, normalized_text FROM document_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchall() == [(active_chunk, "활성 청크")]
            assert conn.execute(
                "SELECT chunk_id, document_id, normalized_text FROM document_chunks_fts"
            ).fetchall() == [
                (active_chunk, str(uuid.UUID(document_id)), "활성 청크")
            ]
            assert "generation_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info('document_chunks')")
            }
            assert "active_chunk_generation_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info('documents')")
            }
            assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        if original_database_env is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", original_database_env)
        get_settings.cache_clear()
        assert get_settings().database_url_sync == original_url


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


def test_migration_disk_preflight_reserves_backup_and_vacuum_space(
    tmp_path, monkeypatch
):
    from app import main

    database = tmp_path / "source.db"
    backups = tmp_path / "backups"
    database.write_bytes(b"x" * 1024)
    backups.mkdir()
    required = database.stat().st_size * 2 + main.MIGRATION_DISK_SAFETY_BYTES

    monkeypatch.setattr(main, "_disk_free_bytes", lambda _path: required - 1)
    with pytest.raises(RuntimeError, match="디스크 공간이 부족"):
        main._ensure_migration_disk_space(database, backups)

    monkeypatch.setattr(main, "_disk_free_bytes", lambda _path: required)
    main._ensure_migration_disk_space(database, backups)


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
