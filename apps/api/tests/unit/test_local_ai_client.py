"""로컬 AI 클라이언트 단위 테스트 — 127.0.0.1 loopback 서버만 사용(외부 접속 없음).

Ollama의 /api/version, /api/tags, /api/pull, /v1/chat/completions를 흉내 내는 loopback
서버를 띄우고, client가 안전 HTTP 경로로 감지·조회·다운로드·테스트를 하는지 검증한다.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.local_ai import client
from app.services.local_ai import settings as st

# 서버 동작을 테스트가 바꿀 수 있도록 모듈 전역에 둔다.
_STATE: dict = {}


class _OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/api/version":
            if _STATE.get("version_redirect"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/other")
                self.end_headers()
                return
            self._json(200, _STATE.get("version", {"version": "0.6.8"}))
        elif self.path == "/api/tags":
            if _STATE.get("tags_huge"):
                body = b'{"models":[' + b'"x",' * (200 * 1024) + b'"y"]}'  # > STATUS_MAX_BYTES
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, OSError):
                    pass
                return
            self._json(200, _STATE.get("tags", {"models": []}))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        _STATE["last_body"] = raw.decode("utf-8")
        if self.path == "/api/pull":
            self._ndjson(_STATE.get("pull_lines", []))
        elif self.path == "/v1/chat/completions":
            self._json(200, _STATE.get("chat", {
                "choices": [{"message": {"content": "안녕하세요."}}]
            }))
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ndjson(self, lines):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for line in lines:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()


@pytest.fixture
def ollama(monkeypatch):
    _STATE.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(st, "OLLAMA_BASE", base)
    monkeypatch.setattr(st, "OLLAMA_OPENAI_BASE", base + "/v1")
    yield _STATE
    server.shutdown()


class TestStatus:
    def test_ready(self, ollama):
        ollama["version"] = {"version": "0.9.0"}
        assert client.get_status().status == client.STATUS_READY

    def test_incompatible_old_version(self, ollama):
        ollama["version"] = {"version": "0.1.0"}
        s = client.get_status()
        assert s.status == client.STATUS_INCOMPATIBLE

    def test_bad_version_json(self, ollama):
        ollama["version"] = {"nope": True}
        assert client.get_status().status == client.STATUS_ERROR

    def test_not_running_when_unreachable(self, monkeypatch):
        # 아무도 듣지 않는 포트 → 연결 실패 → not_running
        monkeypatch.setattr(st, "OLLAMA_BASE", "http://127.0.0.1:1")
        assert client.get_status().status == client.STATUS_NOT_RUNNING

    def test_external_address_not_called(self, monkeypatch):
        # 안전 HTTP 계층이 요청 직전 호스트를 해석해 소켓 연결 전에 unsafe_address로 막는다
        # → 외부 주소로는 실제 호출이 나가지 않고 error 상태가 된다.
        monkeypatch.setattr(st, "OLLAMA_BASE", "http://93.184.216.34:11434")
        assert client.get_status().status == client.STATUS_ERROR

    def test_redirect_blocked(self, ollama):
        # version 응답의 3xx redirect는 추적하지 않는다 → error
        ollama["version_redirect"] = True
        assert client.get_status().status == client.STATUS_ERROR

    def test_oversize_tags_rejected(self, ollama):
        from app.services.summary.endpoint import SummaryNetworkError

        ollama["tags_huge"] = True
        with pytest.raises(SummaryNetworkError):
            client.list_models()


class TestModels:
    def test_filters_to_allowlist(self, ollama):
        ollama["tags"] = {
            "models": [
                {
                    "name": "qwen3:8b",
                    "digest": "sha256:manifest-a",
                    "size": 100,
                    "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"},
                    "modified_at": "2026-01-01",
                },
                {"name": "llama3:70b", "size": 200},  # allowlist 밖 → 제외
            ]
        }
        models = client.list_models()
        assert [m.name for m in models] == ["qwen3:8b"]
        assert models[0].parameter_size == "8B"
        assert models[0].digest == "sha256:manifest-a"
        assert client.installed_model_digest("qwen3:8b") == "sha256:manifest-a"

    def test_digest_lookup_uses_bounded_catalog_probe(self, monkeypatch):
        observed = {}

        def fake_get_json(url, **kwargs):
            observed["url"] = url
            observed.update(kwargs)
            return {"models": [{"name": "qwen3:8b", "digest": "sha256:bounded"}]}

        monkeypatch.setattr(client, "get_json", fake_get_json)

        assert client.installed_model_digest("qwen3:8b") == "sha256:bounded"
        assert observed == {
            "url": f"{st.OLLAMA_BASE}/api/tags",
            "is_local": True,
            "timeout": st.STATUS_TIMEOUT_SEC,
            "max_response_bytes": st.STATUS_MAX_BYTES,
        }

    def test_empty(self, ollama):
        ollama["tags"] = {"models": []}
        assert client.list_models() == []


class TestConnectionTest:
    def test_success(self, ollama):
        ollama["chat"] = {"choices": [{"message": {"content": "안녕하세요."}}]}
        ok, _ = client.test_model("qwen3:8b")
        assert ok is True

    def test_empty_content_rejected(self, ollama):
        ollama["chat"] = {"choices": [{"message": {"content": "   "}}]}
        ok, _ = client.test_model("qwen3:8b")
        assert ok is False

    def test_thinking_only_rejected(self, ollama):
        ollama["chat"] = {"choices": [{"message": {"content": "<think>음...</think>"}}]}
        ok, _ = client.test_model("qwen3:8b")
        assert ok is False  # thinking만 있으면 빈 응답으로 간주

    def test_unknown_model_rejected(self, ollama):
        ok, _ = client.test_model("llama3:70b")
        assert ok is False

    def test_no_document_in_probe(self, ollama):
        client.test_model("qwen3:8b")
        # 프로브 본문에 문서/질문 원문이 들어가지 않는다
        body = ollama["last_body"]
        assert "문서" not in body
        assert "qwen3:8b" in body  # 모델명은 있어야 함


class TestPull:
    def test_yields_events_and_skips_nonjson(self, ollama):
        ollama["pull_lines"] = [
            '{"status":"pulling manifest"}\n',
            "\n",  # 빈 줄
            "not json\n",  # JSON 아님 → 건너뜀
            '{"status":"downloading","total":100,"completed":40}\n',
            '{"status":"success"}\n',
        ]
        events = list(client.pull_model("qwen3:8b"))
        statuses = [e.get("status") for e in events]
        assert "pulling manifest" in statuses
        assert "success" in statuses
        assert any(e.get("completed") == 40 for e in events)

    def test_rejects_non_allowlist_model(self, ollama):
        from app.services.summary.endpoint import SummaryNetworkError

        with pytest.raises(SummaryNetworkError):
            list(client.pull_model("llama3:70b"))
