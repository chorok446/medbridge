"""백업 보존 정책 — 무한 누적을 막되, 되돌릴 사본은 반드시 남긴다."""

from pathlib import Path

from app.services.system.backups import (
    BACKUP_KEEP,
    BACKUP_TOTAL_BUDGET_BYTES,
    enforce_total_budget,
    prune_backups,
)


def _make(directory: Path, name: str, size: int = 16) -> Path:
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


class TestPruneBackups:
    def test_keeps_only_the_newest(self, tmp_path):
        for stamp in ("20260731-142006", "20260801-045318", "20260801-065312",
                      "20260803-182954", "20260804-015839"):
            _make(tmp_path, f"pre-migration-0.1.0-{stamp}.db")

        removed = prune_backups(tmp_path, "pre-migration-", keep=3)

        left = sorted(p.name for p in tmp_path.glob("*.db"))
        assert len(left) == 3
        assert left == [
            "pre-migration-0.1.0-20260801-065312.db",
            "pre-migration-0.1.0-20260803-182954.db",
            "pre-migration-0.1.0-20260804-015839.db",
        ]
        assert len(removed) == 2

    def test_orders_by_stamp_not_version_string(self, tmp_path):
        """사전순 비교는 버전에서 어긋난다 — "0.10.0" < "0.9.0"이라 최신이 먼저 지워진다."""
        old = _make(tmp_path, "pre-migration-0.9.0-20260101-000000.db")
        new = _make(tmp_path, "pre-migration-0.10.0-20260801-000000.db")

        prune_backups(tmp_path, "pre-migration-", keep=1)

        assert new.exists(), "최신 백업이 버전 문자열 때문에 지워졌다"
        assert not old.exists()

    def test_prefixes_are_counted_separately(self, tmp_path):
        """마이그레이션이 연달아 돌아도 업데이트 직전 백업을 밀어내지 않는다."""
        for stamp in ("20260801-000000", "20260802-000000", "20260803-000000",
                      "20260804-000000"):
            _make(tmp_path, f"pre-migration-0.1.0-{stamp}.db")
        update = _make(tmp_path, "pre-update-0.1.0-20260701-000000.db")

        prune_backups(tmp_path, "pre-migration-", keep=3)

        assert update.exists(), "다른 종류의 백업이 함께 지워졌다"
        assert len(list(tmp_path.glob("pre-migration-*.db"))) == 3

    def test_same_second_suffix_is_ordered(self, tmp_path):
        base = _make(tmp_path, "pre-migration-0.1.0-20260804-015839.db")
        later = _make(tmp_path, "pre-migration-0.1.0-20260804-015839-1.db")

        prune_backups(tmp_path, "pre-migration-", keep=1)

        assert later.exists()
        assert not base.exists()

    def test_unparseable_name_falls_back_to_mtime(self, tmp_path):
        """이름 형식이 다른 파일이 섞여도 터지지 않는다."""
        legacy = _make(tmp_path, "pre-migration-legacy.db")
        current = _make(tmp_path, "pre-migration-0.1.0-20260804-015839.db")

        prune_backups(tmp_path, "pre-migration-", keep=1)

        # 타임스탬프가 있는 쪽이 항상 더 최신으로 취급된다
        assert current.exists()
        assert not legacy.exists()

    def test_nothing_to_prune_is_noop(self, tmp_path):
        kept = _make(tmp_path, "pre-migration-0.1.0-20260804-015839.db")
        assert prune_backups(tmp_path, "pre-migration-", keep=3) == []
        assert kept.exists()

    def test_missing_directory_does_not_raise(self, tmp_path):
        """청소가 실패해도 마이그레이션·업데이트 경로를 막지 않는다."""
        assert prune_backups(tmp_path / "없는디렉터리", "pre-migration-") == []

    def test_undeletable_file_is_skipped_not_raised(self, tmp_path, monkeypatch):
        for stamp in ("20260801-000000", "20260802-000000"):
            _make(tmp_path, f"pre-migration-0.1.0-{stamp}.db")

        def boom(self):
            raise OSError("파일이 잠겨 있음")

        monkeypatch.setattr(Path, "unlink", boom)
        assert prune_backups(tmp_path, "pre-migration-", keep=1) == []
        assert len(list(tmp_path.glob("*.db"))) == 2

    def test_default_keep_is_bounded(self):
        assert 1 <= BACKUP_KEEP <= 5


class TestTotalBudget:
    """개수 상한만으로는 디스크를 지킬 수 없다.

    BACKUP_KEEP=3이 접두사별로 적용되고 접두사가 pre-migration-·pre-update- 둘이라
    정상 상태의 상한이 6개다. 이 모듈 docstring이 '고쳐야 할 상태'로 지목한 바로 그
    개수이고, DB가 2.5GB인 기기에서는 백업만 15GB가 되어 실제 PDF(0.99GB)의 15배를
    차지한다. 되돌리기에 필요한 것은 '직전 사본'이지 '여섯 벌'이 아니다.

    종류별 최신 한 벌은 총량을 넘겨도 지키지 않는다 — 그게 백업의 존재 이유다.
    """

    def test_oldest_go_first_until_under_budget(self, tmp_path):
        # 각 4바이트, 예산 10바이트 → 최신 두 개(8바이트)만 남는다.
        old = _make(tmp_path, "pre-migration-0.1.0-20260801-000000.db", size=4)
        mid = _make(tmp_path, "pre-migration-0.1.0-20260802-000000.db", size=4)
        new = _make(tmp_path, "pre-migration-0.1.0-20260803-000000.db", size=4)

        removed = enforce_total_budget(tmp_path, budget_bytes=10)

        assert removed == [old.name]
        assert not old.exists()
        assert mid.exists() and new.exists()

    def test_latest_of_each_kind_survives_any_budget(self, tmp_path):
        """되돌릴 사본을 예산 때문에 지우면 백업이 아니다."""
        migration = _make(tmp_path, "pre-migration-0.1.0-20260801-000000.db", size=100)
        update = _make(tmp_path, "pre-update-0.1.0-20260802-000000.db", size=100)

        removed = enforce_total_budget(tmp_path, budget_bytes=1)

        assert removed == []
        assert migration.exists() and update.exists()

    def test_counts_both_kinds_together(self, tmp_path):
        # 접두사별로 세면 이 상황이 걸리지 않는다 — 각 2개씩이라 keep=3 안이다.
        _make(tmp_path, "pre-migration-0.1.0-20260801-000000.db", size=4)
        _make(tmp_path, "pre-update-0.1.0-20260801-000000.db", size=4)
        migration_new = _make(tmp_path, "pre-migration-0.1.0-20260803-000000.db", size=4)
        update_new = _make(tmp_path, "pre-update-0.1.0-20260804-000000.db", size=4)

        removed = enforce_total_budget(tmp_path, budget_bytes=10)

        assert len(removed) == 2, removed
        assert migration_new.exists() and update_new.exists()

    def test_under_budget_removes_nothing(self, tmp_path):
        _make(tmp_path, "pre-migration-0.1.0-20260801-000000.db", size=4)

        assert enforce_total_budget(tmp_path, budget_bytes=1000) == []

    def test_missing_directory_does_not_raise(self, tmp_path):
        assert enforce_total_budget(tmp_path / "없는디렉터리") == []

    def test_default_budget_is_bounded(self):
        # 되돌리기용 사본 두 벌은 담되, 실기기에서 본 11.5GB는 막는 크기.
        assert 1 * 1024**3 <= BACKUP_TOTAL_BUDGET_BYTES <= 12 * 1024**3
