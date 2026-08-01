"""로컬 AI 설정 저장의 SQLite 오류 분류 단위 테스트."""

import json
import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

from app.core.logging import correlation_id_var
from app.db.session import SQLITE_BUSY_TIMEOUT_MS, _set_sqlite_pragmas
from app.services.local_ai.service import (
    _persistence_log_fields,
    classify_persistence_failure,
)


class _SqliteError(Exception):
    def __init__(self, code: int, message: str = "redacted") -> None:
        super().__init__(message)
        self.sqlite_errorcode = code


def test_sqlite_pragmas_set_explicit_busy_timeout() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        _set_sqlite_pragmas(connection, None)

        timeout = connection.execute("PRAGMA busy_timeout").fetchone()
    finally:
        connection.close()

    assert timeout == (SQLITE_BUSY_TIMEOUT_MS,)


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_BUSY,
        261,  # SQLITE_BUSY_RECOVERY
        517,  # SQLITE_BUSY_SNAPSHOT
        773,  # SQLITE_BUSY_TIMEOUT
    ],
)
def test_extended_busy_codes_are_retryable(code: int) -> None:
    wrapped = OperationalError(None, {}, _SqliteError(code))

    failure = classify_persistence_failure(wrapped)

    assert failure.category == "db_locked"
    assert failure.retryable is True
    assert failure.sqlite_error_code == code
    assert failure.sqlite_primary_code == sqlite3.SQLITE_BUSY
    assert failure.exception_type == "_SqliteError"


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (sqlite3.SQLITE_LOCKED, "db_locked"),
        (sqlite3.SQLITE_READONLY, "db_readonly"),
        (sqlite3.SQLITE_SCHEMA, "db_schema_mismatch"),
        (sqlite3.SQLITE_CONSTRAINT, "db_integrity"),
        (sqlite3.SQLITE_CORRUPT, "db_integrity"),
        (sqlite3.SQLITE_NOTADB, "db_integrity"),
        (sqlite3.SQLITE_IOERR, "db_io"),
        (sqlite3.SQLITE_FULL, "db_io"),
        (sqlite3.SQLITE_CANTOPEN, "db_io"),
    ],
)
def test_nonbusy_sqlite_codes_are_not_automatic_busy_retries(
    code: int, category: str
) -> None:
    failure = classify_persistence_failure(_SqliteError(code))

    assert failure.category == category
    assert failure.sqlite_primary_code != sqlite3.SQLITE_BUSY


@pytest.mark.parametrize("message", ["no such table: x", "no such column: y"])
def test_generic_sqlite_schema_errors_are_safely_classified(message: str) -> None:
    failure = classify_persistence_failure(_SqliteError(sqlite3.SQLITE_ERROR, message))

    assert failure.category == "db_schema_mismatch"
    assert failure.retryable is False


def test_unknown_exception_does_not_expose_message_in_failure() -> None:
    secret = "C:/Users/name/private/medbridge.db token=do-not-log"

    failure = classify_persistence_failure(RuntimeError(secret))

    assert failure.category == "unknown_persistence_error"
    assert failure.retryable is False
    assert secret not in repr(failure)


def test_structured_report_uses_safe_camel_case_fields() -> None:
    secret = "C:/private/medbridge.db token=secret document=patient-notes"
    failure = classify_persistence_failure(RuntimeError(secret))
    token = correlation_id_var.set("cid-test-123")
    try:
        report = _persistence_log_fields(failure, stage="commit", attempt=1)
    finally:
        correlation_id_var.reset(token)

    assert report["operation"] == "local_ai_activate"
    assert report["failureCategory"] == "unknown_persistence_error"
    assert report["sqliteErrorCode"] is None
    assert report["correlationId"] == "cid-test-123"
    assert report["stage"] == "commit"
    assert secret not in json.dumps(report, default=str)
