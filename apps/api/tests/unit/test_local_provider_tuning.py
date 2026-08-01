"""로컬 provider 요청 튜닝 + thinking 제거 단위 테스트(네트워크 없이 http_client 주입)."""

import json

from app.services.model_output import strip_thinking
from app.services.qa.provider import OpenAICompatibleQaProvider, QaContextChunk, QaRequest
from app.services.summary.provider import OpenAICompatibleSummaryProvider


class _Capture:
    def __init__(self, response: dict):
        self.payload = None
        self._response = response

    def __call__(self, url, payload, api_key):
        self.payload = payload
        return self._response


class TestStripThinking:
    def test_removes_think_block(self):
        assert strip_thinking("<think>추론...</think>정답입니다") == "정답입니다"

    def test_removes_unclosed_think(self):
        assert strip_thinking("<think>끝나지 않은 추론") == ""

    def test_keeps_plain_text(self):
        assert strip_thinking("그냥 답") == "그냥 답"


class TestQaLocalTuning:
    def _req(self):
        return QaRequest(question="q", chunks=[QaContextChunk("c1", "제목", "본문", 1, 1)])

    def test_local_adds_reasoning_none_and_temp0(self):
        cap = _Capture({"choices": [{"message": {"content": json.dumps(
            {"answer": "a", "answerStatus": "answered", "claims": []})}}]})
        p = OpenAICompatibleQaProvider(
            endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b", api_key="",
            is_local=True, http_client=cap,
        )
        p.answer(self._req())
        assert cap.payload["temperature"] == 0
        assert cap.payload["reasoning_effort"] == "none"
        assert cap.payload["max_tokens"] > 0

    def test_external_keeps_contract(self):
        cap = _Capture({"choices": [{"message": {"content": json.dumps(
            {"answer": "a", "answerStatus": "answered", "claims": []})}}]})
        p = OpenAICompatibleQaProvider(
            endpoint="https://api.example.com/v1", model_name="gpt-x", api_key="k",
            is_local=False, http_client=cap,
        )
        p.answer(self._req())
        assert "reasoning_effort" not in cap.payload  # 외부 계약 유지

    def test_local_strips_thinking_before_json(self):
        # 모델이 thinking을 붙여도 JSON 파싱 전에 제거된다
        content = "<think>고민</think>" + json.dumps(
            {"answer": "a", "answerStatus": "answered", "claims": []})
        cap = _Capture({"choices": [{"message": {"content": content}}]})
        p = OpenAICompatibleQaProvider(
            endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b", api_key="",
            is_local=True, http_client=cap,
        )
        result = p.answer(self._req())
        assert result["answerStatus"] == "answered"


class TestSummaryLocalTuning:
    def test_local_available_without_key(self):
        p = OpenAICompatibleSummaryProvider(
            endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b", api_key="",
            is_local=True,
        )
        assert p.available is True

    def test_external_needs_key(self):
        p = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1", model_name="gpt-x", api_key="",
            is_local=False,
        )
        assert p.available is False
