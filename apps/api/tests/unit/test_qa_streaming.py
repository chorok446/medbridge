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


class TestOllamaNativeStreaming:
    """QA도 요약과 같은 native 경로를 써야 컨텍스트가 지정된다.

    요약 어댑터만 `/api/chat` + `options.num_ctx`로 옮기고 QA 스트리밍은 OpenAI 호환
    `/chat/completions`에 남아 있었다. 그 경로는 num_ctx를 받지 못해 기기별 기본
    컨텍스트(대개 4096)로 모델이 올라가는데, QA 프롬프트는 시스템 계약 + 12,000자
    청크 + 히스토리라 그 창을 넘는다. Ollama는 **앞부분부터** 조용히 잘라내고, 잘리는
    앞부분이 바로 "NDJSON 한 줄씩 내라"는 계약이라 모델이 평범한 산문을 돌려준다.
    파서가 모든 줄을 버려 답이 페이지에 적혀 있어도 매번 "근거를 찾지 못했어요"가
    나오고, temperature 0이라 재시도해도 같다.
    """

    def _capture(self, endpoint: str, is_local: bool, lines):
        seen: dict = {}

        def line_source(url, payload, key):
            seen["url"] = url
            seen["payload"] = payload
            return iter(lines)

        provider = OpenAICompatibleStreamingQaProvider(
            endpoint=endpoint,
            model_name="qwen3:8b",
            api_key="",
            is_local=is_local,
            line_source=line_source,
        )
        return provider, seen

    def test_ollama_gets_native_url_and_num_ctx(self):
        from app.services.summary.settings import LOCAL_NUM_CTX

        lines = [
            '{"message":{"content":"{\\"type\\":\\"final\\",\\"answerStatus\\":\\"answered\\"}\\n"}}'
        ]
        p, seen = self._capture("http://127.0.0.1:11434/v1", True, lines)

        list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))

        assert seen["url"] == "http://127.0.0.1:11434/api/chat"
        assert seen["payload"]["options"]["num_ctx"] == LOCAL_NUM_CTX
        assert seen["payload"]["stream"] is True

    def test_native_frames_are_parsed(self):
        # native 응답은 SSE가 아니라 NDJSON이고 delta 위치도 다르다.
        lines = [
            '{"message":{"content":"{\\"type\\":\\"claim\\",\\"text\\":\\"심장은 뛴다\\","}}',
            '{"message":{"content":"\\"sourceChunkIds\\":[\\"c1\\"]}\\n"}}',
            '{"message":{"content":"{\\"type\\":\\"final\\",\\"answerStatus\\":\\"answered\\"}\\n"},"done":true}',
        ]
        p, _ = self._capture("http://127.0.0.1:11434/v1", True, lines)

        events = list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))

        assert events[0]["type"] == "claim"
        assert events[0]["text"] == "심장은 뛴다"
        assert events[-1]["type"] == "final"

    def test_external_provider_stays_on_openai_path(self):
        # 외부 공급자에게 Ollama 전용 필드를 보내면 400으로 거절당한다.
        lines = _sse('{"type":"final","answerStatus":"answered"}\n')
        p, seen = self._capture("https://api.example.com/v1", False, lines)

        list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))

        assert seen["url"] == "https://api.example.com/v1/chat/completions"
        assert "options" not in seen["payload"]

    def test_non_ollama_local_server_stays_on_openai_path(self):
        # LM Studio·llama.cpp server는 loopback이지만 /api/chat이 없다.
        lines = _sse('{"type":"final","answerStatus":"answered"}\n')
        p, seen = self._capture("http://127.0.0.1:1234/v1", True, lines)

        list(p.stream_answer(QaRequest(question="q", chunks=[]), _Token()))

        assert seen["url"] == "http://127.0.0.1:1234/v1/chat/completions"
        assert "options" not in seen["payload"]


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
