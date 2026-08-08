"""sidecar 런타임 관리 — 단일 실행 잠금·업데이트 정지 지점·백업.

단일 실행은 커널이 관리하는 파일 잠금(flock/msvcrt)으로 보장한다:
프로세스가 죽으면 잠금이 자동 해제되므로 stale PID 추정이 필요 없고 TOCTOU가 없다.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import IO

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.paths import get_path_provider
from app.services.system.backups import (
    enforce_total_budget,
    next_backup_path,
    prune_backups,
)

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


def reject_if_updating() -> None:
    """업데이트 준비 중에는 새 문서 작업(업로드·추출·OCR)을 시작하지 않는다.

    문구·상태코드가 호출부마다 갈라지지 않게 여기 한 곳에서만 정의한다.
    """
    from app.core.errors import AppError, ErrorCode

    if _updating:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "업데이트를 준비하는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=503,
            retryable=True,
        )


@contextlib.contextmanager
def operation():
    """진행 중인 문서 작업(업로드 등) 추적 — 업데이트 정지 지점 계산에 사용.

    수락 검사(reject_if_updating)와 카운터 증가는 같은 이벤트 루프 틱에서 일어나므로
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

    provider.backups_dir.mkdir(parents=True, exist_ok=True)
    backup = next_backup_path(provider.backups_dir, "pre-update-", __version__)

    # SQLite의 백업 API로 뜬다. shutil.copy2는 파일을 그냥 읽어 복사하므로, 수 GB DB를
    # 복사하는 수십 초~수 분 동안 다른 요청이 DB에 쓰면 사본이 찢어진다 — 앞부분과
    # 뒷부분이 서로 다른 시점이 되어 'database disk image is malformed'로 열리지
    # 않는다. 되돌릴 목적으로 만든 백업이 되돌릴 수 없는 파일이 되는 것이 최악이다.
    #
    # reject_if_updating은 업로드·추출·OCR **시작**만 막고, Q&A 답변 저장·스레드
    # 삭제·설정 저장 같은 쓰기는 그대로 진행된다. 그 경로를 전부 잠그는 대신 SQLite가
    # 이미 제공하는 것을 쓴다: `.backup()`은 복사 중 원본이 바뀌면 바뀐 페이지를 다시
    # 읽어 일관된 스냅샷을 만든다. 쓰기를 막지 않으므로 사용자 작업도 멈추지 않는다.
    with sqlite3.connect(db_path) as src:
        # 먼저 WAL을 본체로 접어 넣는다 — 사본이 WAL 파일 없이도 완결되게.
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
    logger.info("pre_update_backup_created", backup=backup.name)
    # 새 백업이 자리 잡은 뒤에 정리한다 — 먼저 지우면 복사가 실패했을 때
    # 되돌릴 사본만 없앤 꼴이 된다.
    prune_backups(provider.backups_dir, "pre-update-")
    enforce_total_budget(provider.backups_dir)
    return backup.name
