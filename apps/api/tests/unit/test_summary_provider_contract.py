"""요약 모델의 bounded JSON·서버 소유 출처 계약 회귀 테스트."""

import json

import pytest

from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.provider import (
    ChunkInput,
    DocumentRequest,
    GroupRequest,
    GroupSummary,
    OpenAICompatibleSummaryProvider,
)
from app.services.summary.settings import (
    GROUP_SUMMARY_MAX_CHARS,
    SUMMARY_MAP_MAX_TOKENS,
)
from app.services.tasks.summary_job import _classify_pipeline_failure


class _Capture:
    def __init__(self, response: dict):
        self.response = response
        self.payloads: list[dict] = []
        self.calls: list[tuple[str, dict, str]] = []  # (url, payload, api_key)

    def __call__(self, url, payload, api_key):
        self.payloads.append(payload)
        self.calls.append((url, payload, api_key))
        return self.response


def _provider(response: dict) -> tuple[OpenAICompatibleSummaryProvider, _Capture]:
    capture = _Capture(response)
    provider = OpenAICompatibleSummaryProvider(
        endpoint="https://api.example.com/v1",
        model_name="test-model",
        api_key="secret",
        is_local=False,
        http_client=capture,
    )
    return provider, capture


def _group_request() -> GroupRequest:
    return GroupRequest(
        group_id="g0",
        section_title="순환계",
        chunks=[
            ChunkInput("private-c1", "순환계", "심장은 혈액을 보낸다.", 1, 1),
            ChunkInput("private-c1", "순환계", "심장은 혈액을 보낸다.", 1, 1),
            ChunkInput("private-c2", "순환계", "혈관은 혈액의 통로다.", 1, 1),
        ],
        learner_level="nursing_student",
        language="ko",
    )


def _chat_response(content: str, *, finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {"finish_reason": finish_reason, "message": {"content": content}}
        ]
    }


def test_map_is_concise_and_sources_are_server_owned():
    content = json.dumps(
        {"summary": "심장과 혈관의 역할을 설명한다.", "sourceChunkIds": ["forged"]}
    )
    provider, capture = _provider(_chat_response(content))

    result = provider.summarize_group(_group_request())

    assert result.summary_text == "심장과 혈관의 역할을 설명한다."
    assert result.source_chunk_ids == ["private-c1", "private-c2"]
    payload = capture.payloads[0]
    assert payload["max_tokens"] == SUMMARY_MAP_MAX_TOKENS == 512
    prompt = "\n".join(message["content"] for message in payload["messages"])
    assert "private-c1" not in prompt
    assert "private-c2" not in prompt
    assert "sourceChunkIds" not in prompt
    assert "[입력 1]" in prompt


def test_finish_reason_length_is_invalid_even_when_content_is_json():
    provider, _ = _provider(
        _chat_response(json.dumps({"summary": "완성처럼 보임"}), finish_reason="length")
    )

    with pytest.raises(SummaryNetworkError) as caught:
        provider.summarize_group(_group_request())

    assert caught.value.category == "bad_response"


@pytest.mark.parametrize(
    "content",
    [
        '{"summary":"끝나지 않음',
        json.dumps([{"summary": "객체 아님"}]),
        json.dumps({"summary": ""}),
        json.dumps({"summary": "가" * 401}),
        json.dumps({"summary": 123}),
    ],
)
def test_map_rejects_malformed_or_out_of_contract_content(content):
    provider, _ = _provider(_chat_response(content))

    with pytest.raises(SummaryNetworkError) as caught:
        provider.summarize_group(_group_request())

    assert caught.value.category == "bad_response"


class TestFailureIsDiagnosable:
    """bad_response 하나에 7가지 원인이 뭉쳐 있으면 실기기 로그로 원인을 좁힐 수 없다.

    각 계약 위반이 서로 다른 reason을 남기는지 고정한다. reason에는 문서·모델 원문이
    들어가면 안 된다(분류값만).
    """

    @pytest.mark.parametrize(
        ("content", "finish_reason", "expected_reason"),
        [
            (json.dumps({"summary": "정상"}), "length", "finish_length"),
            (json.dumps({"summary": "정상"}), "content_filter", "finish_content_filter"),
            (json.dumps({"summary": "정상"}), "무작위값", "finish_other"),
            ('{"summary":"끊김', "stop", "map_content_not_json"),
            (json.dumps([1, 2]), "stop", "map_content_not_object"),
            (json.dumps({"summary": 123}), "stop", "map_summary_missing"),
            (json.dumps({"summary": "   "}), "stop", "map_summary_empty"),
            (json.dumps({"summary": "가" * 401}), "stop", "map_summary_too_long"),
        ],
    )
    def test_map_failures_carry_distinct_reasons(self, content, finish_reason, expected_reason):
        provider, _ = _provider(_chat_response(content, finish_reason=finish_reason))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "bad_response"
        assert caught.value.reason == expected_reason

    def test_unknown_finish_reason_is_not_echoed_verbatim(self):
        """모델이 준 임의 문자열을 로그에 그대로 싣지 않는다."""
        provider, _ = _provider(
            _chat_response(json.dumps({"summary": "정상"}), finish_reason="비밀경로 C:/x")
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "finish_other"
        assert "비밀경로" not in str(caught.value)

    def test_reduce_group_id_failures_are_distinguishable(self):
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]
        request = DocumentRequest(
            group_summaries=groups, learner_level="nursing_student", language="ko"
        )
        shape_broken = {"overview": {"text": "개요", "sourceGroupIds": "g0"}}
        unknown_id = {"overview": {"text": "개요", "sourceGroupIds": ["g99"]}}

        with pytest.raises(SummaryNetworkError) as shape:
            OpenAICompatibleSummaryProvider._resolve_group_sources(shape_broken, request)
        with pytest.raises(SummaryNetworkError) as unknown:
            OpenAICompatibleSummaryProvider._resolve_group_sources(unknown_id, request)

        assert shape.value.reason == "reduce_group_ids_shape"
        assert unknown.value.reason == "reduce_group_ids_unknown"


class TestContextOverflow:
    """실기기 대형 문서 실패의 확정 원인.

    그룹이 모델 컨텍스트 창을 넘으면 Ollama가 프롬프트를 조용히 자르고(HTTP 200),
    모델은 summary 대신 error 객체를 돌려준다. 이걸 "응답 형식 오류"로 뭉뚱그리면
    사용자는 무엇을 고쳐야 할지 알 수 없다.
    """

    # 실측 응답(qwen3:8b, num_ctx=2048에서 6000자 그룹 투입)
    REAL_OVERFLOW = (
        "Input is too long or contains excessive repetition. "
        "Please provide a concise and clear question or statement for assistance."
    )

    def test_truncated_prompt_error_is_reported_as_context_overflow(self):
        provider, _ = _provider(_chat_response(json.dumps({"error": self.REAL_OVERFLOW})))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "context_overflow"
        assert caught.value.reason == "map_context_overflow"

    def test_context_overflow_maps_to_actionable_failure_category(self):
        code, category = _classify_pipeline_failure(
            SummaryNetworkError("context_overflow", "map_context_overflow")
        )
        assert code == "SUMMARY_CONTEXT_OVERFLOW"
        assert category == "context_overflow"

    def test_unrelated_error_object_is_not_mislabelled_as_overflow(self):
        """모든 error 응답을 컨텍스트 초과로 몰아가면 진짜 원인을 가린다."""
        provider, _ = _provider(_chat_response(json.dumps({"error": "model not loaded"})))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "bad_response"
        assert caught.value.reason == "map_summary_missing"

    def test_overflow_message_is_not_echoed_to_user(self):
        """모델이 낸 영문 원문을 사용자 메시지로 그대로 쓰지 않는다."""
        err = SummaryNetworkError("context_overflow", "map_context_overflow")
        assert self.REAL_OVERFLOW not in err.user_message
        assert "크기를 넘었습니다" in err.user_message

    def test_overflow_message_does_not_tell_user_to_change_context(self):
        """앱이 num_ctx를 직접 지정하므로 '컨텍스트를 늘리라'는 안내는 효과가 없다."""
        err = SummaryNetworkError("context_overflow", "map_context_overflow")
        assert "컨텍스트 크기를 늘" not in err.user_message

    def test_error_alternative_is_allowed_by_map_schema(self):
        """스키마가 error 형태를 막으면 잘린 프롬프트가 정상 요약으로 통과한다.

        실측: strict 스키마 + 부족한 num_ctx → 키=['summary'](오탐지 없음),
        anyOf 허용 → 키=['error'](초과 감지됨).
        """
        from app.services.summary.provider import _map_schema

        shapes = _map_schema()["anyOf"]
        required = [set(s["required"]) for s in shapes]
        assert {"summary"} in required
        assert {"error"} in required


class TestLocalNativePath:
    """로컬은 Ollama native /api/chat을 쓴다.

    OpenAI 호환 endpoint로는 컨텍스트를 지정할 수도 조회할 수도 없어, Ollama가 기기별로
    잡은 값에 요약 성패가 좌우됐다(실기기 실패의 원인). native는 num_ctx를 직접 지정하고
    JSON schema로 응답 구조를 강제한다.
    """

    def _local(self, response: dict):
        capture = _Capture(response)
        provider = OpenAICompatibleSummaryProvider(
            endpoint="http://127.0.0.1:11434/v1",
            model_name="qwen3:8b",
            api_key="",
            is_local=True,
            http_client=capture,
        )
        return provider, capture

    @staticmethod
    def _native_response(content: str, *, done_reason: str = "stop") -> dict:
        return {"message": {"content": content}, "done_reason": done_reason}

    def test_local_uses_native_endpoint_with_explicit_context(self):
        from app.services.summary.settings import LOCAL_KEEP_ALIVE, LOCAL_NUM_CTX

        provider, capture = self._local(
            self._native_response(json.dumps({"summary": "요약"}))
        )

        provider.summarize_group(_group_request())

        url, payload, _key = capture.calls[0]
        assert url == "http://127.0.0.1:11434/api/chat"
        assert payload["options"]["num_ctx"] == LOCAL_NUM_CTX
        assert payload["options"]["num_predict"] == SUMMARY_MAP_MAX_TOKENS
        assert payload["think"] is False
        assert payload["keep_alive"] == LOCAL_KEEP_ALIVE
        assert payload["stream"] is False

    def test_map_response_shape_is_enforced_by_schema(self):
        provider, capture = self._local(
            self._native_response(json.dumps({"summary": "요약"}))
        )

        provider.summarize_group(_group_request())

        schema = capture.calls[0][1]["format"]
        success = next(s for s in schema["anyOf"] if "summary" in s["properties"])
        assert success["required"] == ["summary"]
        assert success["properties"]["summary"]["type"] == "string"

    def test_document_schema_requires_object_overview_with_sources(self):
        """overview를 문자열로 돌려주던 실측 문제를 스키마로 막는다."""
        provider, capture = self._local(
            self._native_response(
                json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}})
            )
        )
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        provider.summarize_document(
            DocumentRequest(
                group_summaries=groups, learner_level="nursing_student", language="ko"
            )
        )

        schema = capture.calls[0][1]["format"]
        doc = next(s for s in schema["anyOf"] if "overview" in s.get("properties", {}))
        overview = doc["properties"]["overview"]
        assert overview["type"] == "object"
        assert set(overview["required"]) == {"text", "sourceGroupIds"}
        assert "overview" in doc["required"]
        # 빈 배열을 허용하면 서버가 문서 전체를 실패시킨다 — 스키마가 먼저 막아야 한다
        assert overview["properties"]["sourceGroupIds"]["minItems"] == 1

    def test_no_length_limit_reaches_the_grammar(self):
        """maxLength는 위반을 거부하지 않고 그 지점에서 문자열을 강제로 닫는다.

        GBNF는 maxLength를 `char{0,N}` + 닫는 따옴표로 만들므로, 모델이 상한보다 길게
        쓰려 하면 문장 중간에서 잘린 채 문법상 유효한 JSON이 되어 서버 길이 검증까지
        통과한다("…투여 금기다" → "…투여 금"). map 요약에서는 그 때문에
        map_summary_too_long이 영영 발생하지 않아 적응 분할이 도달 불가능해진다.
        길이는 서버가 검증한다.
        """
        from app.services.summary.provider import _document_schema, _map_schema

        def has_max_length(node) -> bool:
            if isinstance(node, dict):
                return "maxLength" in node or any(has_max_length(v) for v in node.values())
            if isinstance(node, list):
                return any(has_max_length(v) for v in node)
            return False

        assert not has_max_length(_map_schema())
        assert not has_max_length(
            _document_schema(include_sections=True, include_prerequisites=True)
        )

    def test_over_long_map_summary_still_fails_the_contract(self):
        """문법이 자르지 않으므로 계약 위반이 서버까지 도달해 분할 신호가 된다."""
        provider, _ = self._local(
            self._native_response(json.dumps({"summary": "가" * (GROUP_SUMMARY_MAX_CHARS + 1)}))
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "map_summary_too_long"

    def test_truncated_native_prompt_is_detected_without_model_confession(self):
        """Ollama는 프롬프트를 조용히 자르고 모델은 대개 그럴듯한 요약을 만들어 낸다.

        문구 매칭에만 의존하면 잘린 입력의 부분 요약이 그룹 전체 출처를 달고 성공으로
        저장된다. 서버가 num_ctx를 지정했으므로 prompt_eval_count로 확정할 수 있다.
        """
        from app.services.summary.settings import LOCAL_NUM_CTX

        response = self._native_response(json.dumps({"summary": "그럴듯한 요약"}))
        response["prompt_eval_count"] = LOCAL_NUM_CTX
        provider, _ = self._local(response)

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "context_overflow"
        assert caught.value.reason == "native_prompt_truncated"

    def test_loopback_hostname_still_takes_the_native_path(self):
        """localhost:11434도 같은 Ollama다 — 문자열 일치로 걸러지면 이번 수정이 통째로
        적용되지 않는다(num_ctx 지정·schema 강제 모두).
        """
        capture = _Capture(self._native_response(json.dumps({"summary": "요약"})))
        provider = OpenAICompatibleSummaryProvider(
            endpoint="http://localhost:11434/v1",
            model_name="qwen3:8b",
            api_key="",
            is_local=True,
            http_client=capture,
        )

        assert provider.uses_ollama_native is True
        provider.summarize_group(_group_request())
        assert capture.calls[0][0] == "http://localhost:11434/api/chat"

    def test_reduce_budget_reaches_local_non_ollama_servers(self):
        """LM Studio 등 로컬 OpenAI 호환 서버도 reduce 출력 예산을 받아야 한다.

        기본값(2048)으로 나가면 실측 completion 1,500에서 조금만 길어져도
        finish_length로 문서 전체가 영구 실패한다(temperature 0이라 재시도도 무의미).
        """
        from app.services.summary.settings import SUMMARY_REDUCE_MAX_TOKENS

        capture = _Capture(
            _chat_response(json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}}))
        )
        provider = OpenAICompatibleSummaryProvider(
            endpoint="http://localhost:1234/v1",  # LM Studio
            model_name="local-model",
            api_key="",
            is_local=True,
            http_client=capture,
        )

        provider.summarize_document(
            DocumentRequest(
                group_summaries=[GroupSummary("g0", "구역", "요약", ["c0"])],
                learner_level="nursing_student",
                language="ko",
            )
        )

        assert capture.calls[0][1]["max_tokens"] == SUMMARY_REDUCE_MAX_TOKENS

    def test_schema_omits_sections_when_not_requested(self):
        provider, capture = self._local(
            self._native_response(
                json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}})
            )
        )
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        provider.summarize_document(
            DocumentRequest(
                group_summaries=groups,
                learner_level="nursing_student",
                language="ko",
                include_sections=False,
                include_prerequisites=False,
            )
        )

        schema = capture.calls[0][1]["format"]
        doc = next(s for s in schema["anyOf"] if "overview" in s.get("properties", {}))
        assert "sections" not in doc["properties"]
        assert "prerequisites" not in doc["properties"]

    def test_native_done_reason_is_checked_like_finish_reason(self):
        provider, _ = self._local(
            self._native_response(json.dumps({"summary": "정상"}), done_reason="length")
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "finish_length"

    def test_non_ollama_local_server_keeps_openai_path(self):
        """is_local은 'loopback이다'이지 'Ollama다'가 아니다.

        LM Studio·llama.cpp server·vLLM은 /api/chat을 제공하지 않으므로 native로 보내면
        404 → '모델을 찾을 수 없습니다'라는 엉뚱한 안내로 깨진다.
        """
        capture = _Capture(_chat_response(json.dumps({"summary": "요약"})))
        provider = OpenAICompatibleSummaryProvider(
            endpoint="http://localhost:1234/v1",  # LM Studio
            model_name="local-model",
            api_key="",
            is_local=True,
            http_client=capture,
        )

        assert provider.uses_ollama_native is False
        provider.summarize_group(_group_request())
        assert capture.calls[0][0].endswith("/chat/completions")

    def test_reduce_does_not_send_max_tokens_to_external_provider(self):
        """외부 reduce는 원래 max_tokens를 보내지 않았다.

        4096을 보내면 completion 상한이 더 낮은 모델에서 400을 받아 모든 문서가 실패한다.
        """
        provider, capture = _provider(
            _chat_response(json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}}))
        )
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        provider.summarize_document(
            DocumentRequest(
                group_summaries=groups, learner_level="nursing_student", language="ko"
            )
        )

        assert "max_tokens" not in capture.calls[0][1]

    def test_external_provider_still_uses_openai_endpoint(self):
        """외부 공급자 계약은 건드리지 않는다 — native는 Ollama 전용이다."""
        provider, capture = _provider(
            _chat_response(json.dumps({"summary": "요약"}))
        )

        provider.summarize_group(_group_request())

        url, payload, _key = capture.calls[0]
        assert url.endswith("/chat/completions")
        assert "options" not in payload
        assert "keep_alive" not in payload
        assert payload["max_tokens"] == SUMMARY_MAP_MAX_TOKENS


def test_group_size_fits_the_context_we_request():
    """그룹 상한이 우리가 지정하는 num_ctx에 출력 여유까지 포함해 들어가야 한다.

    실측(qwen3:8b, 한국어): 프롬프트 토큰 ≈ 문자수 x 0.79. 이 여유가 무너지면 native
    경로에서도 프롬프트가 잘린다.
    """
    from app.services.summary.settings import GROUP_MAX_CHARS, LOCAL_NUM_CTX

    korean_tokens_per_char = 0.79
    instruction_overhead_tokens = 150
    estimated = (
        GROUP_MAX_CHARS * korean_tokens_per_char
        + instruction_overhead_tokens
        + SUMMARY_MAP_MAX_TOKENS
    )
    assert estimated < LOCAL_NUM_CTX, f"추정 {estimated:.0f}토큰 > num_ctx {LOCAL_NUM_CTX}"


def test_reduce_output_budget_fits_the_context_we_request():
    """구조화 reduce도 프롬프트 + 출력이 num_ctx 안에 들어가야 한다."""
    from app.services.summary.settings import (
        GROUP_SUMMARY_MAX_CHARS,
        LOCAL_NUM_CTX,
        REDUCE_FAN_IN,
        SUMMARY_REDUCE_MAX_TOKENS,
    )

    prompt_chars = GROUP_SUMMARY_MAX_CHARS * REDUCE_FAN_IN
    estimated = prompt_chars * 0.79 + 300 + SUMMARY_REDUCE_MAX_TOKENS
    assert estimated < LOCAL_NUM_CTX, f"추정 {estimated:.0f}토큰 > num_ctx {LOCAL_NUM_CTX}"


class TestReduceContract:
    """reduce가 계약을 벗어난 응답을 조용히 통과시키면 '빈 요약'이 SUCCEEDED로 저장된다."""

    def test_truncated_reduce_is_reported_as_context_overflow(self):
        overflow = {"error": "Input is too long or contains excessive repetition."}
        provider, _ = _provider(_chat_response(json.dumps(overflow)))
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(
                DocumentRequest(
                    group_summaries=groups, learner_level="nursing_student", language="ko"
                )
            )

        assert caught.value.category == "context_overflow"
        assert caught.value.reason == "reduce_context_overflow"

    def test_reduce_without_any_content_is_rejected_not_silently_empty(self):
        provider, _ = _provider(_chat_response(json.dumps({"sections": []})))
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(
                DocumentRequest(
                    group_summaries=groups, learner_level="nursing_student", language="ko"
                )
            )

        assert caught.value.reason == "reduce_empty_summary"

    def test_string_overview_does_not_pass_the_empty_summary_guard(self):
        """키 존재만 보면 '실측에서 반복된' 문자열 overview가 그대로 통과한다.

        build_artifacts는 dict가 아닌 overview를 통째로 버리므로, 통과시키면 artifact
        0건인 요약이 SUCCEEDED·100%로 저장돼 사용자는 '완료'라 적힌 빈 패널을 본다.
        """
        provider, _ = _provider(_chat_response(json.dumps({"overview": "그냥 문자열 개요"})))
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(
                DocumentRequest(
                    group_summaries=groups, learner_level="nursing_student", language="ko"
                )
            )

        assert caught.value.reason == "reduce_empty_summary"

    def test_missing_overview_does_not_discard_valid_sections(self):
        """overview만 빠졌다고 문서 전체를 실패시키면 쓸 수 있는 요약까지 버린다.

        로컬은 temperature 0 그리디 디코딩이라 재시도해도 같은 응답이 나와, 수백 회
        map 호출로 만든 중간 결과가 있어도 그 문서는 영구히 요약을 얻지 못한다.
        """
        content = json.dumps(
            {"sections": [{"title": "구역", "summary": "내용", "sourceGroupIds": ["g0"]}]}
        )
        provider, _ = _provider(_chat_response(content))
        groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

        result = provider.summarize_document(
            DocumentRequest(
                group_summaries=groups, learner_level="nursing_student", language="ko"
            )
        )

        assert result["sections"][0]["sourceChunkIds"] == ["c0"]


def test_common_refusal_is_not_misread_as_context_overflow():
    """외부 모델의 평범한 거절을 컨텍스트 초과로 몰면 유료 API를 분할로 낭비한다."""
    from app.services.summary.provider import _looks_like_context_overflow

    assert not _looks_like_context_overflow(
        {"error": "I cannot summarize this without more context"}
    )
    assert _looks_like_context_overflow(
        {"error": "Input is too long or contains excessive repetition."}
    )


def test_reduce_prompt_pins_object_shape_so_overview_is_not_dropped():
    """실측에서 qwen3:8b가 overview를 문자열로 돌려줘 개요가 통째로 사라졌다.

    출처 없는 항목은 저장하지 않는 계약이라 조용히 유실된다 — 프롬프트가 형태를
    예시로 못박아야 한다.
    """
    content = json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}})
    provider, capture = _provider(_chat_response(content))
    groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

    provider.summarize_document(
        DocumentRequest(group_summaries=groups, learner_level="nursing_student", language="ko")
    )

    prompt = "\n".join(m["content"] for m in capture.payloads[0]["messages"])
    assert '"overview":{"text"' in prompt  # 객체 형태를 명시
    assert '"sourceGroupIds"' in prompt
    assert "g0" in prompt  # 고를 수 있는 group id 목록을 알려준다
    assert "c0" not in prompt  # 실제 chunk id는 모델에 보내지 않는다


def test_reduce_prompt_omits_items_the_user_turned_off():
    """예시는 스키마가 강제되지 않는 경로에서 유일한 계약이다.

    끈 항목을 예시로 남기면 모델이 그대로 채워 보내고, 사용자가 명시적으로 끈 구역
    요약·선수지식이 화면에 그대로 뜬다.
    """
    content = json.dumps({"overview": {"text": "개요", "sourceGroupIds": ["g0"]}})
    provider, capture = _provider(_chat_response(content))
    groups = [GroupSummary("g0", "구역", "요약", ["c0"])]

    provider.summarize_document(
        DocumentRequest(
            group_summaries=groups,
            learner_level="nursing_student",
            language="ko",
            include_sections=False,
            include_prerequisites=False,
        )
    )

    prompt = "\n".join(m["content"] for m in capture.payloads[0]["messages"])
    assert '"sections"' not in prompt
    assert '"prerequisites"' not in prompt
    assert '"keyConcepts"' in prompt  # 끄지 않은 항목은 그대로 남는다


def test_reduce_resolves_only_server_group_tokens_to_real_chunk_ids():
    structured = {
        "overview": {
            "text": "문서 개요",
            "sourceGroupIds": ["g1", "g0", "g1"],
            "sourceChunkIds": ["forged-but-valid-looking"],
        },
        "sections": [],
        "keyConcepts": [],
        "prerequisites": [],
        "learnerExplanations": [],
        "studyCautions": [],
    }
    provider, capture = _provider(_chat_response(json.dumps(structured)))
    request = DocumentRequest(
        group_summaries=[
            GroupSummary("g0", None, "첫 요약", ["real-c1"]),
            GroupSummary("g1", None, "둘째 요약", ["real-c2", "real-c1"]),
        ],
        learner_level="nursing_student",
        language="ko",
    )

    result = provider.summarize_document(request)

    assert result["overview"]["sourceChunkIds"] == ["real-c2", "real-c1"]
    assert "sourceGroupIds" not in result["overview"]
    prompt = "\n".join(message["content"] for message in capture.payloads[0]["messages"])
    assert "real-c1" not in prompt
    assert "real-c2" not in prompt
    assert "[group g0]" in prompt


def test_reduce_rejects_unknown_group_token():
    provider, _ = _provider(
        _chat_response(
            json.dumps(
                {"overview": {"text": "개요", "sourceGroupIds": ["not-a-group"]}}
            )
        )
    )
    request = DocumentRequest(
        group_summaries=[GroupSummary("g0", None, "요약", ["real-c1"])],
        learner_level="nursing_student",
        language="ko",
    )

    with pytest.raises(SummaryNetworkError) as caught:
        provider.summarize_document(request)

    assert caught.value.category == "bad_response"


@pytest.mark.parametrize(
    "category,expected",
    [
        ("timeout", ("SUMMARY_TIMEOUT", "timeout")),
        ("bad_response", ("SUMMARY_INVALID_RESPONSE", "invalid_response")),
        ("response_too_large", ("SUMMARY_INVALID_RESPONSE", "invalid_response")),
        ("auth_failed", ("SUMMARY_PROVIDER_ERROR", "provider_error")),
    ],
)
def test_async_failure_categories_are_safe(category, expected):
    assert _classify_pipeline_failure(SummaryNetworkError(category)) == expected
