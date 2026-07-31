"""저장소·배포 안전장치 — 사용자 데이터가 저장소·설치 경로와 분리돼야 한다."""

import subprocess
from pathlib import Path

from app.core.paths import get_path_provider

REPO_ROOT = Path(__file__).resolve().parents[4]  # medbridge/


class TestUserDataOutsideRepo:
    def test_app_data_dir_is_outside_repo(self):
        root = get_path_provider().root.resolve()
        assert not root.is_relative_to(REPO_ROOT)

    def test_no_repo_local_data_dirs(self):
        """./uploads, ./data, ./storage 같은 저장소 내부 데이터 경로를 만들지 않는다."""
        for forbidden in ("uploads", "data", "storage"):
            assert not (REPO_ROOT / forbidden).exists()

    def test_standard_subdirectories(self):
        provider = get_path_provider()
        provider.ensure_directories()
        for d in (
            provider.documents_dir,
            provider.backups_dir,
            provider.logs_dir,
            provider.cache_dir,
        ):
            assert d.is_dir()
            assert d.parent == provider.root


class TestEnvNotTracked:
    def test_env_is_gitignored(self):
        for name in (".env", ".env.local", ".env.production", ".env.pilot"):
            result = subprocess.run(
                ["git", "check-ignore", "-q", name], cwd=REPO_ROOT, capture_output=True
            )
            assert result.returncode == 0, f"{name} 이 .gitignore에 없습니다"

    def test_env_example_is_tracked_pattern(self):
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"], cwd=REPO_ROOT, capture_output=True
        )
        assert result.returncode != 0, ".env.example은 추적돼야 합니다"
