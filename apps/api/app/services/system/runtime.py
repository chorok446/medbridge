"""sidecar 런타임 관리 — 중복 실행 방지·stale 정리·업데이트 준비.

runtime/sidecar.json 에 {pid, port} 를 기록해 두 번째 sidecar 기동을 거부하고,
죽은 프로세스가 남긴 파일은 자동 정리한다.
"""

import contextlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.paths import get_path_provider

logger = get_logger(__name__)

_updating = False


def runtime_file() -> Path:
    runtime_dir = get_path_provider().root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / "sidecar.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def acquire_single_instance(port: int) -> None:
    """이미 살아있는 sidecar가 있으면 기동을 거부한다. stale 파일은 정리."""
    path = runtime_file()
    if path.is_file():
        try:
            info = json.loads(path.read_text())
            old_pid = int(info.get("pid", -1))
        except (ValueError, json.JSONDecodeError):
            old_pid = -1
        if old_pid > 0 and old_pid != os.getpid() and _pid_alive(old_pid):
            raise RuntimeError(f"another sidecar is already running (pid={old_pid})")
        logger.info("stale_runtime_file_cleaned", old_pid=old_pid)
    path.write_text(json.dumps({"pid": os.getpid(), "port": port}))


def release_single_instance() -> None:
    with contextlib.suppress(OSError):
        runtime_file().unlink(missing_ok=True)


def is_updating() -> bool:
    return _updating


def set_updating(value: bool) -> None:
    global _updating
    _updating = value


def checkpoint_and_backup() -> str | None:
    """업데이트 전 안전 처리: WAL checkpoint 후 DB를 backups/에 복사.

    반환: 백업 파일명 (DB 파일이 없으면 None).
    """
    provider = get_path_provider()
    sync_url = get_settings().database_url_sync
    if not sync_url.startswith("sqlite:///"):
        return None
    db_path = Path(sync_url.removeprefix("sqlite:///"))
    if not db_path.is_file():
        return None

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    provider.backups_dir.mkdir(parents=True, exist_ok=True)
    backup = provider.backups_dir / f"pre-update-{__version__}-{stamp}.db"
    suffix = 1
    while backup.exists():  # 같은 초에 재실행돼도 기존 백업을 덮어쓰지 않는다
        backup = provider.backups_dir / f"pre-update-{__version__}-{stamp}-{suffix}.db"
        suffix += 1
    shutil.copy2(db_path, backup)
    logger.info("pre_update_backup_created", backup=backup.name)
    return backup.name
