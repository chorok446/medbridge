"""백업 보존 정책 — 무한 누적을 막되, 되돌릴 사본은 반드시 남긴다."""

from pathlib import Path

from app.services.system.backups import BACKUP_KEEP, prune_backups


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
