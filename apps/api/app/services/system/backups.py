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

# 백업 전체가 차지해도 되는 총량.
#
# 개수만으로는 디스크를 지킬 수 없다. BACKUP_KEEP이 접두사별로 적용되고 접두사가
# 둘이라 정상 상태의 상한이 6개인데, 이건 이 모듈 docstring이 '고쳐야 할 상태'로
# 지목한 바로 그 개수다. DB가 2.5GB인 기기라면 6개가 15GB이고, 같은 기기의 실제
# PDF(0.99GB)의 15배다. 사본 크기는 DB에 비례해 자라므로 개수 상한은 총량을 전혀
# 묶지 못한다.
#
# 8GiB로 잡는 근거: 실기기 DB 2.5GB 기준 되돌리기용 두 벌(마이그레이션·업데이트
# 직전)이 5GB로 들어가고, 실측된 11.5GB는 막는다.
BACKUP_TOTAL_BUDGET_BYTES = 8 * 1024**3

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


def next_backup_path(directory: Path, prefix: str, version: str) -> Path:
    """`{prefix}{version}-{stamp}.db` 형식의 미사용 백업 경로를 만든다.

    이름 형식은 _STAMP_RE 파싱(보존 정렬)과 한 몸이다 — 형식을 바꾸려면 여기 한 곳만
    고치면 되고, 흩어진 복사본이 어긋나 보존 정렬이 mtime 폴백으로 떨어지는 일을 막는다.
    """
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = directory / f"{prefix}{version}-{stamp}.db"
    suffix = 1
    while backup.exists():  # 같은 초에 재실행돼도 기존 백업을 덮어쓰지 않는다
        backup = directory / f"{prefix}{version}-{stamp}-{suffix}.db"
        suffix += 1
    return backup


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


def enforce_total_budget(
    directory: Path, budget_bytes: int = BACKUP_TOTAL_BUDGET_BYTES
) -> list[str]:
    """백업 전체가 `budget_bytes`를 넘지 않게 오래된 것부터 지운다.

    종류별 **최신 한 벌**은 예산을 넘겨도 지키지 않는다 — 되돌릴 사본을 용량 때문에
    지우면 그건 백업이 아니다. 그래서 이 함수는 총량을 반드시 예산 아래로 낮춘다고
    약속하지 않는다. 지울 수 있는 것을 다 지워도 남는 두 벌이 예산보다 크면, 그건
    디스크가 아니라 DB가 커진 문제다.

    `prune_backups`(개수)와 함께 쓴다. 개수만으로는 총량이 묶이지 않는데, 사본 크기가
    DB에 비례해 자라기 때문이다.

    실패해도 예외를 올리지 않는다 — 청소는 마이그레이션·업데이트를 막을 이유가 없다.
    """
    try:
        files = [p for p in directory.glob("pre-*.db") if p.is_file()]
        sized = [(p, p.stat().st_size) for p in files]
    except OSError:
        logger.warning("backup_budget_scan_failed")
        return []
    if not sized:
        return []

    # 종류별 최신 한 벌은 건드리지 않는다.
    protected: set[Path] = set()
    for prefix in {_kind_prefix(p.name) for p, _ in sized}:
        same_kind = [p for p, _ in sized if _kind_prefix(p.name) == prefix]
        protected.add(max(same_kind, key=_sort_key))

    total = sum(size for _, size in sized)
    removed: list[str] = []
    # 오래된 것부터 지운다.
    for path, size in sorted(sized, key=lambda pair: _sort_key(pair[0])):
        if total <= budget_bytes:
            break
        if path in protected:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("backup_budget_prune_failed", backup=path.name)
            continue
        total -= size
        removed.append(path.name)

    if removed:
        logger.info(
            "backups_budget_pruned",
            removed=len(removed),
            remaining_bytes=total,
            budget_bytes=budget_bytes,
        )
    return removed


def _kind_prefix(name: str) -> str:
    """`pre-migration-0.1.0-...db` → `pre-migration-`. 모르는 이름은 통째로 한 종류로."""
    for prefix in ("pre-migration-", "pre-update-"):
        if name.startswith(prefix):
            return prefix
    return name
