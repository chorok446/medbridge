"""Q&A 스트리밍 공급자·프로토콜 단위 테스트 (네트워크 없이)."""

import json

from app.services.qa import stream_protocol as sp
from app.services.qa.context import QaChunkRef
from app.services.qa.provider import QaContextChunk, QaRequest
from app.services.qa.schema import verify_claim_event
from app.services.qa.streaming import (
    DeterministicStreamingQaProvider,
    OpenAICompatibleStreamingQaProvider,
)


class _Token:
    def __init__(self, cancelled=False):
        self._c = cancelled

    def is_cancelled(self):
        return self._c


def _sse(*frames: str) -> list[str]:
    """OpenAI chat.completion.chunk delta 프레임을 SSE data 줄로 만든다."""
    out = []
    for content in frames:
        obj = {"choices": [{"delta": {"content": content}}]}
        out.append(f"data: {json.dumps(obj, ensure_ascii=False)}")
    out.append("data: [DONE]")
    return out


class TestProtocol:
    def test_encode_escapes_newlines(self):
        raw = sp.encode_event(sp.claim_event(1, 0, "줄1\n줄2", []))
        # 한 이벤트는 정확히 한 줄(끝의 개행 1개만)
        assert raw.count(b"\n") == 1
        parsed = json.loads(raw.decode("utf-8"))
        assert parsed["text"] == "줄1\n줄2"

    def test_public_source_ref_drops_internal(self):
        ref = sp.public_source_ref(
            {"pageNumber": 2, "bbox": [1, 2, 3, 4], "blockId": "b", "sectionTitle": "s",
             "sourceMethod": "digital", "chunkId": "SECRET"}
        )
        assert "chunkId" not in ref
        assert ref["pageNumber"] == 2


class TestDeterministicStreaming:
    def test_yields_claims_then_final(self):
        p = DeterministicStreamingQaProvider()
        events = list(
            p.stream_answer(
                QaRequest(
                    question="q",
                    chunks=[QaContextChunk("c1", "제목", "심장은 혈액을 보낸다", 1, 1)],
                ),
                _Token(),
            )
        )
        assert events[0]["type"] == "claim"
        assert events[0]["sourceChunkIds"] == ["c1"]
        assert events[-1]["type"] == "final"

    def test_cancelled_stops_early(self):
        p = DeterministicStreamingQaProvider()
        events = list(
            p.stream_answer(
                QaRequest(question="q", chunks=[QaContextChunk("c1", None, "본문", 1, 1)]),
                _Token(cancelled=True),
            )
        )
        # 즉시 취소 → claim 없이 종료(또는 final만)
        assert all(e["type"] != "claim" for e in events)

    def test_empty_chunks_not_found(self):
        p = DeterministicStreamingQaProvider()
        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))
        assert events[-1]["answerStatus"] == "not_found"


class TestClaimVerification:
    def _lookup(self, text: str):
        ref = {"pageNumber": 1, "bbox": [0, 0, 1, 1], "blockId": "b", "sourceMethod": "digital"}
        return {"c1": QaChunkRef("c1", "제목", text, "h", [ref])}

    def test_grounded_claim_supported(self):
        lookup = self._lookup("심장은 혈액을 온몸으로 보낸다")
        vc = verify_claim_event(
            {"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert vc is not None

    def test_negation_flip_not_supported(self):
        # 원문은 긍정("보낸다")인데 claim이 부정("보내지 않는다")으로 뒤집힘 → 미검증
        lookup = self._lookup("심장은 혈액을 온몸으로 보낸다")
        vc = verify_claim_event(
            {"text": "심장은 혈액을 보내지 않는다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert vc is None

    def test_negation_allowed_when_source_also_negates(self):
        # 원문에도 부정이 있으면 부정 claim 허용
        lookup = self._lookup("이 약은 통증을 줄이지 않는다")
        event = {"text": "이 약은 통증을 줄이지 않는다", "sourceChunkIds": ["c1"]}
        vc = verify_claim_event(event, lookup, claim_index=0)
        assert vc is not None


class TestOpenAIStreaming:
    def _provider(self, lines):
        return OpenAICompatibleStreamingQaProvider(
            endpoint="https://api.example.com/v1",
            model_name="gpt-x",
            api_key="k",
            is_local=False,
            line_source=lambda url, payload, key: iter(lines),
        )

    def test_parses_ndjson_across_token_deltas(self):
        # 하나의 claim JSON이 여러 토큰 delta로 쪼개져 도착
        lines = _sse('{"type":"cla', 'im","text":"심장은 뛴다","sourceCh', 'unkIds":["c1"]}\n',
                     '{"type":"final","answerStatus":"answered"}\n')
        p = self._provider(lines)
        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))
        assert events[0]["type"] == "claim"
        assert events[0]["text"] == "심장은 뛴다"
        assert events[0]["sourceChunkIds"] == ["c1"]
        assert events[-1]["type"] == "final"

    def test_done_terminates(self):
        lines = _sse('{"type":"claim","text":"a","sourceChunkIds":["c1"]}\n')
        p = self._provider(lines)
        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))
        assert any(e["type"] == "claim" for e in events)

    def test_ignores_non_semantic_lines(self):
        lines = _sse('그냥 자유 텍스트\n', '{"type":"final","answerStatus":"answered"}\n')
        p = self._provider(lines)
        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))
        # 자유 텍스트 줄은 무시, final만
        assert [e["type"] for e in events] == ["final"]

    def test_available_flag(self):
        p = self._provider([])
        assert p.available is True

    def test_local_provider_available_without_api_key(self):
        # 로컬(Ollama 등)은 API 키 없이도 사용 가능해야 한다
        p = OpenAICompatibleStreamingQaProvider(
            endpoint="http://127.0.0.1:11434/v1", model_name="llama", api_key="",
            is_local=True,
        )
        assert p.available is True

    def test_external_provider_needs_api_key(self):
        p = OpenAICompatibleStreamingQaProvider(
            endpoint="https://api.example.com/v1", model_name="gpt-x", api_key="",
            is_local=False,
        )
        assert p.available is False
