"""백업 보존 정책.

`pre-migration-*`은 마이그레이션이 적용될 때마다, `pre-update-*`은 업데이트 준비마다
DB **전체**를 복사한다. 지우는 코드가 없어서 실기기에서 6개 11.5GB까지 쌓였다(같은
기기의 실제 PDF는 0.99GB였다). 백업의 목적은 "직전 상태로 되돌리기"이므로 오래된
사본을 무한히 들고 있을 이유가 없다.

종류별로 따로 센다. 합쳐서 세면 마이그레이션이 몇 번 연달아 돌 때 업데이트 직전
백업이 밀려나 정작 되돌려야 할 시점의 사본이 사라진다.
"""

import re
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

# 남길 개수. 되돌리기는 사실상 직전 것만 쓰지만, 한 번의 실패가 곧바로 마지막 사본을
# 소진하지 않도록 여유를 둔다.
BACKUP_KEEP = 3

# `pre-migration-0.1.0-20260804-015839.db`, 같은 초 충돌 시 `...-015839-1.db`
_STAMP_RE = re.compile(r"-(\d{8})-(\d{6})(?:-(\d+))?\.db$")


def _sort_key(path: Path) -> tuple:
    """파일명에 박힌 타임스탬프 기준. 파싱이 안 되면 mtime으로 물러선다.

    이름 전체를 사전순으로 비교하면 버전 문자열에서 어긋난다("0.10.0" < "0.9.0").
    """
    match = _STAMP_RE.search(path.name)
    if match is None:
        return (0, path.stat().st_mtime, 0, 0)
    date, time, suffix = match.groups()
    return (1, int(date), int(time), int(suffix or 0))


def prune_backups(directory: Path, prefix: str, keep: int = BACKUP_KEEP) -> list[str]:
    """`prefix`로 시작하는 백업 중 최신 `keep`개만 남긴다. 지운 파일명을 돌려준다.

    실패해도 예외를 올리지 않는다 — 이건 청소일 뿐이고, 여기서 터지면 마이그레이션·
    업데이트 같은 정작 중요한 경로가 통째로 막힌다.
    """
    try:
        backups = sorted(
            (p for p in directory.glob(f"{prefix}*.db") if p.is_file()),
            key=_sort_key,
            reverse=True,
        )
    except OSError:
        logger.warning("backup_prune_scan_failed", prefix=prefix)
        return []

    removed: list[str] = []
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError:
            # Windows에서 다른 프로세스가 잡고 있으면 지워지지 않는다. 다음 기회에 지운다.
            logger.warning("backup_prune_failed", backup=stale.name)
            continue
        removed.append(stale.name)

    if removed:
        logger.info(
            "backups_pruned", prefix=prefix, removed=len(removed), kept=min(len(backups), keep)
        )
    return removed
