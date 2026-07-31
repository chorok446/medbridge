"""업데이트 준비·오류 보고·단일 실행 (Sprint 1.5)."""

import json
import os

import pytest

from app.core.paths import get_path_provider
from app.services.system import runtime
from tests.conftest import make_pdf
from tests.integration.conftest import drain_jobs


def upload_kwargs(data: bytes, filename: str = "test.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


class TestPrepareUpdate:
    @pytest.fixture(autouse=True)
    def reset_updating(self):
        yield
        runtime.set_updating(False)

    async def test_blocks_new_uploads_until_resume(self, client):
        res = await client.post("/api/system/prepare-update")
        assert res.status_code == 200
        assert res.json()["data"]["ready"] is True

        blocked = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        assert blocked.status_code == 503
        assert "업데이트" in blocked.json()["error"]["message"]

        assert (await client.post("/api/system/resume")).status_code == 204
        ok = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        assert ok.status_code == 201
        await drain_jobs()

    async def test_creates_pre_update_backup(self, client):
        backups = get_path_provider().backups_dir
        before = set(p.name for p in backups.glob("pre-update-*.db"))
        res = await client.post("/api/system/prepare-update")
        backup_file = res.json()["data"]["backupFile"]
        assert backup_file is not None
        assert backup_file.startswith("pre-update-")
        after = set(p.name for p in backups.glob("pre-update-*.db"))
        assert backup_file in after - before


class TestErrorReport:
    async def test_report_shape_and_no_filenames(self, client):
        await client.post(
            "/api/documents", **upload_kwargs(make_pdf(), "환자기록_비밀문서.pdf")
        )
        await drain_jobs()
        res = await client.get("/api/system/error-report")
        assert res.status_code == 200
        report = res.json()["data"]
        assert report["sidecarVersion"]
        assert report["migrationRevision"] == "0001"
        assert "documentStatusCounts" in report
        # 원본 파일명·PDF 내용은 보고서에 포함되지 않는다
        raw = json.dumps(report, ensure_ascii=False)
        assert "환자기록_비밀문서" not in raw


class TestSingleInstance:
    def test_duplicate_start_is_rejected(self):
        runtime.acquire_single_instance(1111)
        try:
            with pytest.raises(RuntimeError):
                # 살아있는 pid(자기 자신을 다른 pid처럼 기록)로 중복 기동 시뮬레이션
                path = runtime.runtime_file()
                path.write_text(json.dumps({"pid": os.getppid(), "port": 1111}))
                runtime.acquire_single_instance(2222)
        finally:
            runtime.release_single_instance()

    def test_stale_runtime_file_is_cleaned(self):
        path = runtime.runtime_file()
        path.write_text(json.dumps({"pid": 99999999, "port": 1234}))  # 죽은 pid
        runtime.acquire_single_instance(5678)
        try:
            info = json.loads(path.read_text())
            assert info["pid"] == os.getpid()
            assert info["port"] == 5678
        finally:
            runtime.release_single_instance()

    def test_release_removes_file(self):
        runtime.acquire_single_instance(4321)
        runtime.release_single_instance()
        assert not runtime.runtime_file().is_file()


class TestVersionConsistency:
    def test_check_versions_script_passes(self):
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[4]
        result = subprocess.run(
            ["python3", "scripts/check-versions.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
