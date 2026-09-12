"""로컬 AI 온보딩 API 통합 테스트 — status·models·test·activate·pull.

네트워크는 client 계층을 monkeypatch해 격리한다(실제 Ollama·외부 접속 없음).
"""

import json
import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db import session as db_session
from app.db.session import get_session_factory
from app.models.summary import SummarySettings
from app.models.user import User
from app.services.local_ai import client as ollama_client
from app.services.local_ai import pull_registry, system
from app.services.local_ai import service as local_service
from app.services.local_ai.client import InstalledModel, OllamaStatus


class TestStatus:
    async def test_ready(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "get_status", lambda: OllamaStatus("ready", "0.9.0"))
        res = await client.get("/api/local-ai/status")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "ready"

    async def test_not_running(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "get_status", lambda: OllamaStatus("not_running"))
        res = await client.get("/api/local-ai/status")
        assert res.json()["data"]["status"] == "not_running"


class TestModels:
    async def test_lists_catalog_with_installed_and_default(self, client, monkeypatch):
        monkeypatch.setattr(
            ollama_client, "list_models",
            lambda: [InstalledModel("qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01")],
        )
        monkeypatch.setattr(system, "total_ram_bytes", lambda: 32 * 1024**3)
        monkeypatch.setattr(system, "free_disk_bytes", lambda path="/": 200 * 1024**3)
        res = await client.get("/api/local-ai/models")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["defaultModel"] == "qwen3:8b"
        by_model = {m["model"]: m for m in data["models"]}
        assert by_model["qwen3:8b"]["installed"] is True
        assert by_model["qwen3:8b"]["recommended"] is True
        assert by_model["qwen3:4b"]["installed"] is False
        # 32GB → 8B 권장, 14B·30B-A3B 선택 가능
        assert by_model["qwen3:8b"]["ramAdvice"] == "recommended"
        assert by_model["qwen3:14b"]["ramAdvice"] == "selectable"
        assert by_model["qwen3:30b-a3b"]["ramAdvice"] == "selectable"

    async def test_nominal_32gb_machine_is_still_selectable(self, client, monkeypatch):
        # 공칭 32GB 기기의 OS 보고값은 예약 메모리 때문에 32GiB에 못 미친다(~31.5GiB).
        # 문서가 "32GB 이상이면 선택 가능"이라 안내하는 바로 그 기기를 경고로 내몰면 안 된다.
        monkeypatch.setattr(ollama_client, "list_models", lambda: [])
        monkeypatch.setattr(system, "total_ram_bytes", lambda: int(31.5 * 1024**3))
        monkeypatch.setattr(system, "free_disk_bytes", lambda path="/": 200 * 1024**3)
        res = await client.get("/api/local-ai/models")
        by_model = {m["model"]: m for m in res.json()["data"]["models"]}
        assert by_model["qwen3:14b"]["ramAdvice"] == "selectable"
        assert by_model["qwen3:30b-a3b"]["ramAdvice"] == "selectable"

    async def test_30b_a3b_warns_below_32gb_ram(self, client, monkeypatch):
        # 가중치만 19GB라 32GB 미만에서는 실행이 어렵다 — 경고로 안내한다.
        monkeypatch.setattr(ollama_client, "list_models", lambda: [])
        monkeypatch.setattr(system, "total_ram_bytes", lambda: 16 * 1024**3)
        monkeypatch.setattr(system, "free_disk_bytes", lambda path="/": 200 * 1024**3)
        res = await client.get("/api/local-ai/models")
        by_model = {m["model"]: m for m in res.json()["data"]["models"]}
        assert by_model["qwen3:30b-a3b"]["ramAdvice"] == "warn"

    async def test_disk_shortage_flags_model(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "list_models", lambda: [])
        monkeypatch.setattr(system, "total_ram_bytes", lambda: 16 * 1024**3)
        monkeypatch.setattr(system, "free_disk_bytes", lambda path="/": 1 * 1024**3)  # 부족
        res = await client.get("/api/local-ai/models")
        by_model = {m["model"]: m for m in res.json()["data"]["models"]}
        assert by_model["qwen3:14b"]["diskOk"] is False


class TestConnectionTest:
    async def test_ok(self, client, monkeypatch):
        monkeypatch.setattr(
            ollama_client, "test_model", lambda m: (True, "로컬 AI를 사용할 준비가 됐습니다.")
        )
        res = await client.post("/api/local-ai/test", json={"model": "qwen3:8b"})
        assert res.status_code == 200
        assert res.json()["data"]["ok"] is True

    async def test_rejects_non_allowlist(self, client):
        res = await client.post("/api/local-ai/test", json={"model": "llama3:70b"})
        assert res.status_code == 422


def _installed(*names):
    return lambda: [InstalledModel(n, 100, "8B", "Q4_K_M", "2026-01-01") for n in names]


@pytest.fixture
async def short_sqlite_busy_timeout(monkeypatch):
    """실제 lock 테스트가 production의 5초를 기다리지 않게 새 pool 연결만 짧게 설정한다."""
    monkeypatch.setattr(db_session, "SQLITE_BUSY_TIMEOUT_MS", 25)
    await db_session.get_engine().dispose()
    yield
    # 25ms 연결을 pool에서 제거한다. monkeypatch 복원 뒤 다음 연결은 production 값을 쓴다.
    await db_session.get_engine().dispose()


async def _seed_profile() -> None:
    async with get_session_factory()() as session:
        session.add(User())
        await session.commit()


def _hold_write_lock() -> sqlite3.Connection:
    db_path = get_settings().app_data_dir / "medbridge.db"
    connection = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    return connection


class TestActivate:
    async def test_saves_local_settings_without_keyring(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["isLocal"] is True
        assert data["enabled"] is True
        assert data["modelName"] == "qwen3:8b"
        # 요약 설정 뷰에서도 로컬로 반영, API 키는 저장되지 않는다
        s = await client.get("/api/settings/summary")
        sd = s.json()["data"]
        assert sd["isLocal"] is True
        assert sd["hasApiKey"] is False
        assert sd["endpoint"].endswith("/v1")

    async def test_reactivating_existing_local_settings_is_idempotent(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=True,
                    provider_type="openai_compatible",
                    endpoint="http://127.0.0.1:11434/v1",
                    model_name="qwen3:8b",
                    is_local=True,
                )
            )
            await session.commit()

        first = await client.post(
            "/api/local-ai/activate", json={"model": "qwen3:8b"}
        )
        second = await client.post(
            "/api/local-ai/activate", json={"model": "qwen3:8b"}
        )

        assert first.status_code == 200
        assert second.status_code == 200
        async with get_session_factory()() as session:
            rows = (await session.execute(select(SummarySettings))).scalars().all()
        assert len(rows) == 1
        assert rows[0].enabled is True
        assert rows[0].provider_type == "openai_compatible"
        assert rows[0].endpoint == "http://127.0.0.1:11434/v1"
        assert rows[0].model_name == "qwen3:8b"
        assert rows[0].is_local is True

    async def test_rejects_non_allowlist(self, client):
        res = await client.post("/api/local-ai/activate", json={"model": "evil:latest"})
        assert res.status_code == 422

    async def test_rejects_uninstalled_model(self, client, monkeypatch):
        # 설치되지 않은 모델은 활성화 불가(요약/Q&A가 깨지지 않게) → 409
        monkeypatch.setattr(ollama_client, "list_models", _installed())  # 아무것도 설치 안 됨
        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        assert res.status_code == 409

    async def test_disabled_external_still_requires_confirm(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        # 꺼져 있어도(enabled=False) 외부 설정이 저장돼 있으면 덮어쓰기 확인을 받는다
        async with get_session_factory()() as s:
            s.add(SummarySettings(
                enabled=False, provider_type="openai_compatible",
                endpoint="https://api.example.com/v1", model_name="gpt-x", is_local=False,
            ))
            await s.commit()
        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        assert res.status_code == 409

    async def test_external_overwrite_requires_confirm(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        # 기존 외부 설정 삽입
        async with get_session_factory()() as s:
            s.add(SummarySettings(
                enabled=True, provider_type="openai_compatible",
                endpoint="https://api.example.com/v1", model_name="gpt-x", is_local=False,
            ))
            await s.commit()
        # 확인 없이 → 409
        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        assert res.status_code == 409
        # 확인 → 덮어씀
        res2 = await client.post(
            "/api/local-ai/activate", json={"model": "qwen3:8b", "overwriteExternal": True}
        )
        assert res2.status_code == 200
        assert res2.json()["data"]["isLocal"] is True

    async def test_external_conflict_has_distinct_error_code(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=False,
                    provider_type="openai_compatible",
                    endpoint="https://api.example.com/v1",
                    model_name="gpt-x",
                    is_local=False,
                )
            )
            await session.commit()

        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})

        assert res.status_code == 409
        assert res.json()["error"]["code"] == ErrorCode.EXTERNAL_AI_OVERWRITE_REQUIRED
        assert res.json()["error"]["details"]["failureCategory"] == (
            "external_settings_conflict"
        )
        assert res.json()["error"]["details"]["conflictType"] == (
            "external_ai_overwrite_required"
        )

    async def test_external_conflict_survives_rollback_failure(self, client, monkeypatch):
        """rollback이 실패해도 409 덮어쓰기 안내가 500으로 바뀌지 않는다.

        409가 500이 되면 프런트가 덮어쓰기 확인 대화상자를 띄우지 못해, 사용자는
        외부 AI를 로컬로 바꿀 방법을 잃는다.
        """
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=True,
                    provider_type="openai_compatible",
                    endpoint="https://api.example.com/v1",
                    model_name="gpt-x",
                    is_local=False,
                )
            )
            await session.commit()

        original_rollback = AsyncSession.rollback

        async def failing_rollback(self):
            raise RuntimeError("rollback 실패 모의")

        monkeypatch.setattr(AsyncSession, "rollback", failing_rollback)
        try:
            res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        finally:
            monkeypatch.setattr(AsyncSession, "rollback", original_rollback)

        assert res.status_code == 409
        assert res.json()["error"]["code"] == ErrorCode.EXTERNAL_AI_OVERWRITE_REQUIRED
        assert res.json()["error"]["details"]["failureCategory"] == (
            "external_settings_conflict"
        )

    async def test_lock_released_before_retry_succeeds(
        self, client, monkeypatch, short_sqlite_busy_timeout
    ):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        await _seed_profile()
        lock = _hold_write_lock()
        retries = 0

        async def release_lock(_delay: float) -> None:
            nonlocal retries
            retries += 1
            lock.commit()

        monkeypatch.setattr(local_service, "_sleep_before_retry", release_lock)
        try:
            res = await client.post("/api/local-ai/activate", json={"model": "qwen3:8b"})
        finally:
            if lock.in_transaction:
                lock.rollback()
            lock.close()

        assert res.status_code == 200
        assert retries == 1
        async with get_session_factory()() as session:
            rows = (await session.execute(select(SummarySettings))).scalars().all()
        assert len(rows) == 1
        assert rows[0].enabled is True
        assert rows[0].is_local is True
        assert rows[0].model_name == "qwen3:8b"

    async def test_persistent_lock_is_retryable_and_rolls_back_partial_state(
        self, client, monkeypatch, short_sqlite_busy_timeout
    ):
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        await _seed_profile()
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=False,
                    provider_type="openai_compatible",
                    endpoint="https://api.example.com/v1",
                    model_name="gpt-before",
                    is_local=False,
                )
            )
            await session.commit()

        lock = _hold_write_lock()
        retry_wait = AsyncMock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)
        try:
            res = await client.post(
                "/api/local-ai/activate",
                json={"model": "qwen3:8b", "overwriteExternal": True},
            )
        finally:
            if lock.in_transaction:
                lock.rollback()
            lock.close()

        assert res.status_code == 503
        error = res.json()["error"]
        assert error["code"] == ErrorCode.DB_LOCKED
        assert error["retryable"] is True
        assert error["details"] == {
            "failureCategory": "db_locked",
            "stage": "flush",
            "sqliteErrorCode": sqlite3.SQLITE_BUSY,
            "sqlitePrimaryCode": sqlite3.SQLITE_BUSY,
        }
        retry_wait.assert_awaited_once()

        async with get_session_factory()() as session:
            row = (await session.execute(select(SummarySettings))).scalars().one()
        assert row.enabled is False
        assert row.provider_type == "openai_compatible"
        assert row.endpoint == "https://api.example.com/v1"
        assert row.model_name == "gpt-before"
        assert row.is_local is False

    async def test_nonbusy_flush_failure_is_not_retried_and_rolls_back(
        self, monkeypatch
    ):
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=False,
                    provider_type="disabled",
                    endpoint=None,
                    model_name="before",
                    is_local=False,
                )
            )
            await session.commit()

        retry_wait = AsyncMock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)
        async with get_session_factory()() as session:
            monkeypatch.setattr(
                session,
                "flush",
                AsyncMock(side_effect=RuntimeError("injected non-SQLite failure")),
            )
            with pytest.raises(AppError) as caught:
                await local_service.activate(
                    session, "qwen3:8b", overwrite_external=False
                )

        assert caught.value.code == ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable is False
        assert caught.value.details == {
            "failureCategory": "unknown_persistence_error",
            "stage": "flush",
        }
        retry_wait.assert_not_awaited()
        async with get_session_factory()() as session:
            row = (await session.execute(select(SummarySettings))).scalars().one()
        assert row.enabled is False
        assert row.provider_type == "disabled"
        assert row.model_name == "before"
        assert row.is_local is False

    async def test_readonly_connection_is_safely_classified_without_retry(
        self, monkeypatch
    ):
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=False,
                    provider_type="disabled",
                    model_name="before",
                    is_local=False,
                )
            )
            await session.commit()

        retry_wait = AsyncMock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)
        async with get_session_factory()() as session:
            await session.execute(text("PRAGMA query_only=ON"))
            try:
                with pytest.raises(AppError) as caught:
                    await local_service.activate(
                        session, "qwen3:8b", overwrite_external=False
                    )
            finally:
                await session.execute(text("PRAGMA query_only=OFF"))
                await session.commit()

        assert caught.value.code == ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable is False
        assert caught.value.details == {
            "failureCategory": "db_readonly",
            "stage": "flush",
            "sqliteErrorCode": sqlite3.SQLITE_READONLY,
            "sqlitePrimaryCode": sqlite3.SQLITE_READONLY,
        }
        retry_wait.assert_not_awaited()
        async with get_session_factory()() as session:
            row = (await session.execute(select(SummarySettings))).scalars().one()
        assert row.enabled is False
        assert row.provider_type == "disabled"
        assert row.model_name == "before"
        assert row.is_local is False

    async def test_commit_failure_rolls_back_and_logs_only_safe_report(
        self, monkeypatch
    ):
        async with get_session_factory()() as session:
            session.add(
                SummarySettings(
                    enabled=False,
                    provider_type="disabled",
                    model_name="before",
                    is_local=False,
                )
            )
            await session.commit()

        sensitive = "C:/private/medbridge.db token=secret document=patient-notes"
        retry_wait = AsyncMock()
        error_log = Mock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)
        monkeypatch.setattr(
            local_service,
            "logger",
            SimpleNamespace(error=error_log, warning=Mock()),
        )
        async with get_session_factory()() as session:
            real_rollback = session.rollback
            rollback = AsyncMock(wraps=real_rollback)
            monkeypatch.setattr(
                session,
                "commit",
                AsyncMock(side_effect=RuntimeError(sensitive)),
            )
            monkeypatch.setattr(session, "rollback", rollback)
            with pytest.raises(AppError) as caught:
                await local_service.activate(
                    session, "qwen3:8b", overwrite_external=False
                )

        assert caught.value.details == {
            "failureCategory": "unknown_persistence_error",
            "stage": "commit",
        }
        retry_wait.assert_not_awaited()
        rollback.assert_awaited_once()
        error_log.assert_called_once()
        event, report = error_log.call_args.args[0], error_log.call_args.kwargs
        assert event == "local_ai_activate_failed"
        assert report["failureCategory"] == "unknown_persistence_error"
        assert report["stage"] == "commit"
        assert report["correlationId"]
        encoded_report = json.dumps(report, default=str)
        assert sensitive not in encoded_report
        for fragment in ("medbridge.db", "token=secret", "patient-notes"):
            assert fragment not in encoded_report
            assert fragment not in str(caught.value.details)

        async with get_session_factory()() as session:
            row = (await session.execute(select(SummarySettings))).scalars().one()
        assert row.enabled is False
        assert row.provider_type == "disabled"
        assert row.model_name == "before"
        assert row.is_local is False

    async def test_duplicate_settings_are_detected_without_overwriting(
        self, monkeypatch
    ):
        # id를 명시해 싱글턴 PK가 생기기 **전에** 중복이 굳은 설치본을 재현한다.
        # 이제 기본값이 고정 id라 ORM 경로로는 중복을 만들 수 없다.
        async with get_session_factory()() as session:
            session.add_all(
                [
                    SummarySettings(
                        id=uuid.uuid4(),
                        enabled=False,
                        provider_type="disabled",
                        model_name="first",
                        is_local=False,
                    ),
                    SummarySettings(
                        id=uuid.uuid4(),
                        enabled=False,
                        provider_type="openai_compatible",
                        endpoint="https://api.example.com/v1",
                        model_name="second",
                        is_local=False,
                    ),
                ]
            )
            await session.commit()

        retry_wait = AsyncMock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)
        async with get_session_factory()() as session:
            with pytest.raises(AppError) as caught:
                await local_service.activate(
                    session, "qwen3:8b", overwrite_external=True
                )

        assert caught.value.code == ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable is False
        assert caught.value.details == {
            "failureCategory": "duplicate_settings",
            "stage": "load_settings",
        }
        retry_wait.assert_not_awaited()
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(SummarySettings).order_by(SummarySettings.model_name)
                )
            ).scalars().all()
        assert [(row.model_name, row.enabled, row.is_local) for row in rows] == [
            ("first", False, False),
            ("second", False, False),
        ]

    async def test_post_commit_view_failure_is_classified_and_not_retried(
        self, monkeypatch
    ):
        load_calls = 0
        real_load = local_service.load_settings_row

        async def counted_load(session):
            nonlocal load_calls
            load_calls += 1
            return await real_load(session)

        async def fail_view(_session):
            raise RuntimeError("injected view failure")

        monkeypatch.setattr(local_service, "load_settings_row", counted_load)
        monkeypatch.setattr(local_service, "get_settings_view", fail_view)
        retry_wait = AsyncMock()
        monkeypatch.setattr(local_service, "_sleep_before_retry", retry_wait)

        async with get_session_factory()() as session:
            with pytest.raises(AppError) as caught:
                await local_service.activate(
                    session, "qwen3:8b", overwrite_external=False
                )

        assert caught.value.code == ErrorCode.POST_COMMIT_VIEW_FAILED
        assert caught.value.retryable is False
        assert caught.value.details == {
            "failureCategory": "post_commit_view_failed",
            "stage": "reload_settings_view",
            "committed": True,
        }
        assert load_calls == 1
        retry_wait.assert_not_awaited()
        async with get_session_factory()() as session:
            row = (await session.execute(select(SummarySettings))).scalars().one()
        assert row.enabled is True
        assert row.is_local is True
        assert row.model_name == "qwen3:8b"


class TestPull:
    async def test_streams_progress_and_completed(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "get_status", lambda: OllamaStatus("ready", "0.9.0"))

        def _fake_pull(model, should_cancel=None):
            yield {"status": "pulling manifest"}
            yield {"status": "downloading", "total": 100, "completed": 50, "digest": "sha256:x"}
            yield {"status": "success"}

        monkeypatch.setattr(ollama_client, "pull_model", _fake_pull)
        # success 뒤 실제 설치 확인을 통과하도록 설치 목록에 포함
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:8b"))
        events = []
        async with client.stream(
            "POST", "/api/local-ai/models/pull", json={"model": "qwen3:8b"}
        ) as resp:
            assert resp.status_code == 200
            assert "application/x-ndjson" in resp.headers["content-type"]
            async for line in resp.aiter_lines():
                if line.strip():
                    events.append(json.loads(line))
        types = [e["type"] for e in events]
        assert types[-1] == "completed"
        assert any(e["type"] == "progress" and e.get("percent") == 50.0 for e in events)
        # 내부 용어(digest/blob/manifest) 미노출
        assert "digest" not in json.dumps(events)
        assert "sha256" not in json.dumps(events)

    async def test_rejects_non_allowlist(self, client):
        res = await client.post("/api/local-ai/models/pull", json={"model": "llama3:70b"})
        assert res.status_code == 422

    async def test_rejects_when_not_ready(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "get_status", lambda: OllamaStatus("not_running"))
        res = await client.post("/api/local-ai/models/pull", json={"model": "qwen3:8b"})
        assert res.status_code == 409

    async def test_duplicate_pull_blocked(self, client, monkeypatch):
        monkeypatch.setattr(ollama_client, "get_status", lambda: OllamaStatus("ready", "0.9.0"))
        assert pull_registry.try_begin("qwen3:8b") is True  # 이미 진행 중인 상태 흉내
        try:
            res = await client.post("/api/local-ai/models/pull", json={"model": "qwen3:8b"})
            assert res.status_code == 409
        finally:
            pull_registry.finish("qwen3:8b")

    async def test_no_external_provider_settings_lost_on_activate(self, client, monkeypatch):
        # activate가 문서/질문을 다루지 않고 순수 설정만 저장하는지(회귀 방지) — 문서 없이 동작
        monkeypatch.setattr(ollama_client, "list_models", _installed("qwen3:4b"))
        res = await client.post("/api/local-ai/activate", json={"model": "qwen3:4b"})
        assert res.status_code == 200
        async with get_session_factory()() as s:
            rows = (await s.execute(select(SummarySettings))).scalars().all()
        assert len(rows) == 1
        assert rows[0].model_name == "qwen3:4b"
