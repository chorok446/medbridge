"""로컬 AI 온보딩 API 통합 테스트 — status·models·test·activate·pull.

네트워크는 client 계층을 monkeypatch해 격리한다(실제 Ollama·외부 접속 없음).
"""

import json

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.summary import SummarySettings
from app.services.local_ai import client as ollama_client
from app.services.local_ai import pull_registry, system
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
        # 32GB → 8B 권장, 14B 선택 가능
        assert by_model["qwen3:8b"]["ramAdvice"] == "recommended"
        assert by_model["qwen3:14b"]["ramAdvice"] == "selectable"

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
