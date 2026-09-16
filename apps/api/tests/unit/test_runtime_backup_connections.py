"""업데이트 백업은 성공/실패 모두 SQLite 핸들을 반환해야 한다."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.services.system import runtime


@pytest.mark.parametrize("backup_fails", [False, True])
def test_backup_closes_connections_before_return_or_raise(tmp_path, monkeypatch, backup_fails):
    source_path = tmp_path / "source.db"
    with closing(sqlite3.connect(source_path)) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(
        database_url_sync=f"sqlite:///{source_path.as_posix()}"
    ))
    monkeypatch.setattr(runtime, "get_path_provider", lambda: SimpleNamespace(
        backups_dir=tmp_path / "backups"
    ))
    original_connect = sqlite3.connect
    connections = []  # 참조를 유지해 GC 타이밍이 누수를 숨기지 못하게 한다.

    class TrackedConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            if backup_fails:
                raise sqlite3.OperationalError("simulated backup failure")
            return super().backup(target, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(runtime.sqlite3, "connect", connect)
    try:
        if backup_fails:
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                runtime.checkpoint_and_backup()
        else:
            assert runtime.checkpoint_and_backup() is not None
        assert len(connections) == 2
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()
