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
        # 사용자 자유 입력 신고는 zip 포함 대상 로그(sidecar.log)에 남지 않아야 한다
        await client.post(
            "/api/reports",
            json={"description": "환자 김민준씨 파일이 안 열려요"},
        )
        res = await client.get("/api/system/error-report")
        assert res.status_code == 200
        report = res.json()["data"]
        assert "김민준" not in json.dumps(report, ensure_ascii=False)
        assert report["sidecarVersion"]
        assert report["migrationRevision"] == "0012"
        assert "documentStatusCounts" in report
        # 원본 파일명·PDF 내용은 보고서에 포함되지 않는다
        raw = json.dumps(report, ensure_ascii=False)
        assert "환자기록_비밀문서" not in raw


class TestSingleInstance:
    def test_duplicate_lock_is_rejected(self):
        """커널 파일 잠금 — 두 번째 잠금 시도(별도 fd)는 실패해야 한다."""
        runtime.acquire_single_instance(1111)
        try:
            with open(runtime.runtime_dir() / "sidecar.lock", "a+b") as second:
                assert runtime._try_lock(second) is False
        finally:
            runtime.release_single_instance()

    def test_reacquire_after_release(self):
        """프로세스 종료(=해제) 후에는 즉시 다시 기동할 수 있다 — stale 판정 불필요."""
        runtime.acquire_single_instance(4321)
        runtime.release_single_instance()
        runtime.acquire_single_instance(8765)
        try:
            info = json.loads((runtime.runtime_dir() / "sidecar.json").read_text())
            assert info["pid"] == os.getpid()
            assert info["port"] == 8765
        finally:
            runtime.release_single_instance()

    def test_release_removes_info_file(self):
        runtime.acquire_single_instance(4321)
        runtime.release_single_instance()
        assert not (runtime.runtime_dir() / "sidecar.json").is_file()


class TestQuiescence:
    async def test_prepare_waits_for_inflight_operation(self, client):
        """게이트를 닫은 뒤 진행 중이던 요청이 끝나야 prepare가 완료된다."""
        import asyncio

        release = asyncio.Event()

        async def long_operation():
            with runtime.operation():
                await release.wait()

        op_task = asyncio.create_task(long_operation())
        await asyncio.sleep(0)  # operation 등록 보장
        assert runtime.active_operations() == 1

        prepare_task = asyncio.create_task(client.post("/api/system/prepare-update"))
        await asyncio.sleep(0.2)
        assert not prepare_task.done()  # 진행 중 작업이 있는 동안 완료되지 않는다

        release.set()
        await op_task
        res = await asyncio.wait_for(prepare_task, timeout=10)
        assert res.status_code == 200
        runtime.set_updating(False)


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
