"""sidecar 런타임 관리 — 단일 실행 잠금·업데이트 정지 지점·백업.

단일 실행은 커널이 관리하는 파일 잠금(flock/msvcrt)으로 보장한다:
프로세스가 죽으면 잠금이 자동 해제되므로 stale PID 추정이 필요 없고 TOCTOU가 없다.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.paths import get_path_provider
from app.services.system.backups import prune_backups

logger = get_logger(__name__)

_updating = False
_active_operations = 0
_lock_handle: IO[bytes] | None = None


def runtime_dir() -> Path:
    d = get_path_provider().root / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_lock(handle: IO[bytes]) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def acquire_single_instance(port: int) -> None:
    """커널 파일 잠금으로 중복 sidecar 기동을 거부한다. 잠금은 프로세스 종료 시 자동 해제."""
    global _lock_handle
    lock_path = runtime_dir() / "sidecar.lock"
    handle = open(lock_path, "a+b")  # noqa: SIM115  (프로세스 수명 동안 유지)
    if not _try_lock(handle):
        handle.close()
        raise RuntimeError("another sidecar is already running")
    _lock_handle = handle
    # 진단용 정보 파일 (판정에는 쓰지 않는다)
    (runtime_dir() / "sidecar.json").write_text(
        json.dumps({"pid": os.getpid(), "port": port, "version": __version__})
    )


def release_single_instance() -> None:
    global _lock_handle
    if _lock_handle is not None:
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
            _lock_handle.close()
        _lock_handle = None
    with contextlib.suppress(OSError):
        (runtime_dir() / "sidecar.json").unlink(missing_ok=True)


def is_updating() -> bool:
    return _updating


def set_updating(value: bool) -> None:
    global _updating
    _updating = value


@contextlib.contextmanager
def operation():
    """진행 중인 문서 작업(업로드 등) 추적 — 업데이트 정지 지점 계산에 사용.

    수락 검사(_reject_if_updating)와 카운터 증가는 같은 이벤트 루프 틱에서 일어나므로
    게이트를 닫은 뒤 카운터가 0이 되면 새 변경이 없음이 보장된다.
    """
    global _active_operations
    _active_operations += 1
    try:
        yield
    finally:
        _active_operations -= 1


def active_operations() -> int:
    return _active_operations


async def wait_for_quiescence(timeout_seconds: float = 60.0) -> None:
    """진행 중 요청과 백그라운드 작업이 모두 끝날 때까지 대기."""
    from app.services.tasks.runner import get_task_runner

    runner = get_task_runner()
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        await runner.drain()
        if _active_operations == 0 and not runner.has_pending():
            return
        await asyncio.sleep(0.05)
    logger.warning("quiescence_timeout", active=_active_operations)


def checkpoint_and_backup() -> str | None:
    """업데이트 전 안전 처리: WAL checkpoint 후 DB를 backups/에 복사."""
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
    # 새 백업이 자리 잡은 뒤에 정리한다 — 먼저 지우면 복사가 실패했을 때
    # 되돌릴 사본만 없앤 꼴이 된다.
    prune_backups(provider.backups_dir, "pre-update-")
    return backup.name
