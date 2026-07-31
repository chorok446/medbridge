"""저장소·배포 안전장치 검증 — 실사용자 데이터가 git pull에 안전해야 한다."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]  # medbridge/
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()


class TestComposeVolumes:
    def test_named_volumes_declared(self):
        for volume in ("postgres_data:", "redis_data:", "minio_data:"):
            assert volume in COMPOSE

    def test_services_use_named_volumes(self):
        assert "postgres_data:/var/lib/postgresql/data" in COMPOSE
        assert "minio_data:/data" in COMPOSE
        assert "redis_data:/data" in COMPOSE

    def test_no_bind_mount_user_data_paths(self):
        """./uploads, ./data/*, ./storage 같은 저장소 내부 경로를 데이터 저장에 쓰지 않는다."""
        for forbidden in ("./uploads", "./data/postgres", "./data/minio", "./storage"):
            assert forbidden not in COMPOSE


class TestEnvNotTracked:
    def test_env_is_gitignored(self):
        for name in (".env", ".env.local", ".env.production", ".env.pilot"):
            result = subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            assert result.returncode == 0, f"{name} 이 .gitignore에 없습니다"

    def test_env_example_is_tracked_pattern(self):
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        assert result.returncode != 0, ".env.example은 추적돼야 합니다"
