"""Q&A 스트리밍 공급자·프로토콜 단위 테스트 (네트워크 없이)."""

import asyncio
import json

from app.services.qa import stream_protocol as sp
from app.services.qa.context import QaChunkRef
from app.services.qa.provider import QaContextChunk, QaRequest
from app.services.qa.schema import classify_claim_event
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


def _drain(events: list[dict]) -> list[dict]:
    async def _run() -> list[dict]:
        async def _gen():
            for e in events:
                yield e

        return [
            json.loads(chunk.decode("utf-8"))
            async for chunk in sp.bounded(_gen())
        ]

    return asyncio.run(_run())


class TestBounded:
    def test_oversized_terminal_event_is_still_forwarded(self):
        # 출처가 많은 completed 한 줄이 MAX_EVENT_BYTES를 넘어도 버리면 안 된다 —
        # 버리면 스트림이 terminal 없이 닫혀 완료된 답변이 연결 끊김으로 표시된다.
        huge = sp.completed({"content": "가" * sp.MAX_EVENT_BYTES})
        assert len(sp.encode_event(huge)) > sp.MAX_EVENT_BYTES
        out = _drain([sp.phase("answering"), huge])
        assert [e["type"] for e in out] == ["phase", "completed"]

    def test_oversized_non_terminal_event_is_dropped(self):
        huge_claim = sp.claim_event(1, 0, "가" * sp.MAX_EVENT_BYTES, [])
        out = _drain([huge_claim, sp.completed({"content": "done"})])
        assert [e["type"] for e in out] == ["completed"]

    def test_stream_total_cap_finishes_with_error_event(self):
        # 개별 이벤트는 상한 이하("가"는 UTF-8 3바이트), 총합만 MAX_STREAM_BYTES를 넘긴다.
        big = "가" * (sp.MAX_EVENT_BYTES // 6)
        claim = sp.claim_event(0, 0, big, [])
        assert len(sp.encode_event(claim)) <= sp.MAX_EVENT_BYTES
        count = sp.MAX_STREAM_BYTES // len(sp.encode_event(claim)) + 2
        claims = [sp.claim_event(i, i, big, []) for i in range(count)]
        out = _drain(claims + [sp.completed({"content": "done"})])
        assert out[-1]["type"] == "error"
        assert out[-1]["code"] == "QA_STREAM_TOO_LARGE"

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
        vc, reason = classify_claim_event(
            {"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert vc is not None
        assert reason is None

    def test_negation_flip_not_supported(self):
        # 원문은 긍정("보낸다")인데 claim이 부정("보내지 않는다")으로 뒤집힘 → 미검증
        lookup = self._lookup("심장은 혈액을 온몸으로 보낸다")
        vc, reason = classify_claim_event(
            {"text": "심장은 혈액을 보내지 않는다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert vc is None
        assert reason is not None

    def test_negation_allowed_when_source_also_negates(self):
        # 원문에도 부정이 있으면 부정 claim 허용
        lookup = self._lookup("이 약은 통증을 줄이지 않는다")
        event = {"text": "이 약은 통증을 줄이지 않는다", "sourceChunkIds": ["c1"]}
        vc, _reason = classify_claim_event(event, lookup, claim_index=0)
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

    def test_malformed_output_yields_no_claim_safely(self):
        # 사고 과정·자유 텍스트·깨진 JSON만 오면 예외 없이 claim을 전혀 내지 않는다(안전).
        lines = _sse('이건 사고 과정 텍스트\n', '<think>추론</think>\n',
                     '{"type":"claim","text":"미완결', '  깨진 JSON\n')
        p = self._provider(lines)
        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))
        assert all(e["type"] != "claim" for e in events)

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


class TestFinalStatusSafety:
    """스트림 종료 시 서버 최종 상태 확정 — 미검증/상반 근거를 안전하게 처리한다."""

    @staticmethod
    def _sc(text, claim_index=0):
        from types import SimpleNamespace

        # 실제 VerifiedClaim은 항상 claim_index를 갖는다 — 본문 조립이 그 번호로 마커를 단다.
        return SimpleNamespace(text=text, claim_index=claim_index)

    def test_no_supported_with_results_is_insufficient(self):
        from app.models.enums import QaMessageStatus
        from app.services.qa.stream_service import _final_status

        st, _ = _final_status([], "", had_results=True)
        assert st == QaMessageStatus.INSUFFICIENT_EVIDENCE

    def test_no_supported_no_results_is_not_found(self):
        from app.models.enums import QaMessageStatus
        from app.services.qa.stream_service import _final_status

        st, _ = _final_status([], "", had_results=False)
        assert st == QaMessageStatus.NOT_FOUND

    def test_single_supported_is_completed(self):
        from app.models.enums import QaMessageStatus
        from app.services.qa.stream_service import _final_status

        st, _ = _final_status([self._sc("심장은 혈액을 보낸다")], "", had_results=True)
        assert st == QaMessageStatus.COMPLETED

    def test_contradiction_without_final_hint_is_conflicting(self):
        from app.models.enums import QaMessageStatus
        from app.services.qa.stream_service import _final_status

        c1 = "초기 연구에서는 이 요법이 사망 위험을 감소시킨다고 보고하였다"
        c2 = "후속 연구에서는 이 요법이 사망 위험에 영향을 주지 않았다고 보고하였다"
        st, _ = _final_status([self._sc(c1, 0), self._sc(c2, 1)], "", had_results=True)
        assert st == QaMessageStatus.CONFLICTING_EVIDENCE

    def test_unrelated_two_claims_stay_completed(self):
        from app.models.enums import QaMessageStatus
        from app.services.qa.stream_service import _final_status

        st, _ = _final_status(
            [self._sc("심장은 혈액을 보낸다", 0), self._sc("심박수는 분당 범위에 있다", 1)],
            "", had_results=True,
        )
        assert st == QaMessageStatus.COMPLETED
