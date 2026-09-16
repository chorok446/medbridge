"""Layer 2 preflight — allowlist·Ollama/모델 미설치 skip 테스트(네트워크 없이 monkeypatch)."""

import pytest

from app.qa_eval import ollama
from app.services.local_ai import client
from app.services.summary.endpoint import SummaryNetworkError


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a" * 64, "sha256:" + "a" * 64),
        ("sha256:" + "b" * 64, "sha256:" + "b" * 64),
    ],
)
def test_normalizes_ollama_transport_digest(raw, expected):
    assert ollama.normalize_model_digest(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "A" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63, "md5:" + "a" * 64],
)
def test_rejects_missing_or_noncanonical_transport_digest(raw):
    with pytest.raises(ollama.ModelDigestError):
        ollama.normalize_model_digest(raw)


async def test_release_digest_is_read_from_exact_installed_model(monkeypatch):
    monkeypatch.setattr(
        client,
        "list_models",
        lambda **_kwargs: [
            client.InstalledModel("qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01", "c" * 64)
        ],
    )

    digest = await ollama.release_model_digest("qwen3:8b")

    assert digest == "sha256:" + "c" * 64


async def test_release_digest_fails_closed_when_ollama_returns_invalid_value(monkeypatch):
    monkeypatch.setattr(
        client,
        "list_models",
        lambda **_kwargs: [
            client.InstalledModel("qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01", None)
        ],
    )

    with pytest.raises(ollama.ModelDigestError):
        await ollama.release_model_digest("qwen3:8b")


async def test_release_digest_rejects_duplicate_model_tags(monkeypatch):
    duplicate = client.InstalledModel(
        "qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01", "d" * 64
    )
    monkeypatch.setattr(client, "list_models", lambda **_kwargs: [duplicate, duplicate])

    with pytest.raises(ollama.ModelDigestError, match="정확히 하나"):
        await ollama.release_model_digest("qwen3:8b")


async def test_release_digest_retries_transient_catalog_failure(monkeypatch):
    calls = 0

    def list_models(**_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SummaryNetworkError("connect_failed")
        return [
            client.InstalledModel(
                "qwen3:8b", 100, "8B", "Q4_K_M", "2026-01-01", "e" * 64
            )
        ]

    monkeypatch.setattr(client, "list_models", list_models)

    assert await ollama.release_model_digest("qwen3:8b") == "sha256:" + "e" * 64
    assert calls == 3


async def test_loaded_release_digest_uses_bounded_fixed_loopback_ps(monkeypatch):
    captured = {}

    def get_json(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return {"models": [{"name": "qwen3:8b", "digest": "a" * 64}]}

    monkeypatch.setattr(ollama, "get_json", get_json)

    digest = await ollama.loaded_release_model_digest("qwen3:8b")

    assert digest == "sha256:" + "a" * 64
    assert captured == {
        "url": "http://127.0.0.1:11434/api/ps",
        "is_local": True,
        "timeout": 1.0,
        "max_response_bytes": 256 * 1024,
    }


@pytest.mark.parametrize(
    "models",
    [
        [],
        [
            {"name": "qwen3:8b", "digest": "a" * 64},
            {"model": "qwen3:8b", "digest": "a" * 64},
        ],
        [{"name": "qwen3:8b", "digest": "invalid"}],
    ],
)
async def test_loaded_release_digest_fails_closed_for_missing_duplicate_or_invalid(
    monkeypatch, models
):
    monkeypatch.setattr(ollama, "get_json", lambda *_args, **_kwargs: {"models": models})

    with pytest.raises(ollama.ModelDigestError):
        await ollama.loaded_release_model_digest("qwen3:8b")
