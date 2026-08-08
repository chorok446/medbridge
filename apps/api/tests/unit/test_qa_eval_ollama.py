"""Layer 2 preflight — allowlist·Ollama/모델 미설치 skip 테스트(네트워크 없이 monkeypatch)."""

import pytest

from app.qa_eval import ollama
from app.services.local_ai import client


async def test_rejects_non_allowlist_model():
    with pytest.raises(ollama.ModelNotAllowedError):
        await ollama.preflight("llama3:70b")


async def test_skips_when_ollama_not_running(monkeypatch):
    monkeypatch.setattr(client, "get_status", lambda: client.OllamaStatus("not_running"))
    ready, reason = await ollama.preflight("qwen3:8b")
    assert ready is False
    assert "실행" in reason


async def test_skips_when_model_not_installed(monkeypatch):
    monkeypatch.setattr(client, "get_status", lambda: client.OllamaStatus("ready", "0.9.0"))
    monkeypatch.setattr(client, "list_models", lambda: [])  # 설치된 모델 없음
    ready, reason = await ollama.preflight("qwen3:8b")
    assert ready is False
    assert "설치" in reason


async def test_ready_when_installed(monkeypatch):
    monkeypatch.setattr(client, "get_status", lambda: client.OllamaStatus("ready", "0.9.0"))
    monkeypatch.setattr(
        client, "list_models",
        lambda: [client.InstalledModel("qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01")],
    )
    ready, reason = await ollama.preflight("qwen3:8b")
    assert ready is True
    assert reason == ""
