"""요약 모델의 bounded JSON·서버 소유 출처 계약 회귀 테스트."""

import json

import pytest

from app.services.summary import endpoint as endpoint_mod
from app.services.summary import provider as provider_mod
from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.executor import SummaryConsentRevoked, _is_input_too_large
from app.services.summary.provider import (
    ChunkInput,
    DocumentRequest,
    GroupRequest,
    GroupSummary,
    OpenAICompatibleSummaryProvider,
    ProviderRequestBudget,
    new_provider_request_budget,
    provider_checkpoint_fingerprint,
    provider_request_budget_scope,
    providers_share_runtime_configuration,
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
            # 출력 절단은 공급자가 예산을 늘려 한 번 더 부른다. 그래도 잘리면(이 mock은
            # 항상 같은 응답을 준다) 분할 가능한 별도 reason으로 올린다.
            (json.dumps({"summary": "정상"}), "length", "map_finish_length"),
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

    def test_reduce_truncation_is_promoted_so_the_executor_can_adapt(self):
        """reduce가 잘리면 '입력을 줄이면 풀릴 수 있는 실패'로 승격돼야 한다.

        map은 더 큰 예산으로 한 번 더 부르지만 reduce는 이미 num_ctx 상한 근처라
        그럴 수 없다. 날것의 finish_length로 올리면 실행기가 '줄여도 소용없는 실패'로
        분류해 접기 재시도를 한 번도 하지 않고 문서 전체 요약이 첫 절단에서 죽는다.
        """
        provider, _ = _provider(
            _chat_response(json.dumps({"overview": {"text": "잘림"}}), finish_reason="length")
        )
        request = DocumentRequest(
            group_summaries=[GroupSummary("g0", "구역", "요약", ["c0"])],
            learner_level="nursing_student",
            language="ko",
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(request)

        assert caught.value.reason == "reduce_finish_length"
        assert _is_input_too_large(caught.value), "실행기가 접기 재시도를 하지 않는다"


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

    def test_error_object_retry_actually_differs_on_openai_compatible_path(self):
        """복구 재질의가 첫 호출과 같은 요청이면 같은 답만 다시 받는다.

        모델이 초과 징후 없는 error 객체를 주면 '오류 형태를 뺀 문법'으로 한 번 더
        묻는다. 그 구분은 JSON schema로만 표현돼 있는데, OpenAI 호환 경로는 schema를
        보내지 않고 response_format=json_object만 보낸다. LM Studio·llama.cpp·vLLM
        같은 로컬 서버는 temperature 0(그리디)이라 두 번째 호출이 바이트 단위로 같은
        요청이 되고, 같은 error가 돌아와 노드가 죽는다 — 주석이 약속한 구제가 이
        사용자들에게는 닿지 않고 실패할 때마다 호출 비용만 2배가 된다.
        """
        provider, capture = _provider(
            _chat_response(json.dumps({"error": "model refused"}))
        )

        with pytest.raises(SummaryNetworkError):
            provider.summarize_group(_group_request())

        assert len(capture.payloads) == 2, "복구 재질의 자체가 나가지 않았다"
        first, second = capture.payloads
        assert first != second, "재질의가 첫 호출과 동일한 요청이다 — 같은 답만 받는다"

    def test_unrelated_error_object_is_not_mislabelled_as_overflow(self):
        """모든 error 응답을 컨텍스트 초과로 몰아가면 진짜 원인을 가린다."""
        provider, _ = _provider(_chat_response(json.dumps({"error": "model not loaded"})))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "bad_response"
        assert caught.value.reason == "map_summary_missing"

    @pytest.mark.parametrize(
        "message",
        [
            "요약하려면 더 많은 컨텍스트가 필요합니다",
            "컨텍스트가 부족해 답변할 수 없습니다",
        ],
    )
    def test_asking_for_more_context_is_not_overflow(self, message):
        """'컨텍스트가 더 필요하다'는 초과의 반대다.

        영어 목록은 이 오탐을 피하려고 맨 "context"를 일부러 뺐는데(주석에 명시),
        한국어 쪽에 단독 "컨텍스트"를 넣어 같은 오탐을 되살렸다. 초과로 오판하면
        실행기가 그룹을 깊이 3까지 쪼개 노드 하나에 20회 넘는 호출을 태우고, 끝내
        "메모리를 확보하라"는 실행 불가 안내가 뜬다 — 모델이 그냥 거절한 것뿐인데.
        """
        provider, _ = _provider(_chat_response(json.dumps({"error": message})))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category != "context_overflow", caught.value.reason

    @pytest.mark.parametrize(
        "message",
        [
            "입력이 컨텍스트 길이를 초과했습니다",
            "최대 컨텍스트 창을 넘었습니다",
        ],
    )
    def test_korean_overflow_wording_is_still_detected(self, message):
        """한국어로 답하는 모델의 진짜 초과 표현은 계속 잡아야 한다."""
        provider, _ = _provider(_chat_response(json.dumps({"error": message})))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "context_overflow"

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
        """native의 done_reason도 OpenAI finish_reason과 같은 경로를 탄다.

        절단은 공급자가 예산을 늘려 한 번 더 부르므로, 계속 잘리는 mock에서는 분할
        가능한 map_finish_length로 올라온다.
        """
        provider, _ = self._local(
            self._native_response(json.dumps({"summary": "정상"}), done_reason="length")
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "map_finish_length"

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


def test_map_retry_budget_also_fits_the_context_we_request():
    """출력 절단 재시도는 예산을 키운다 — 그 예산까지 num_ctx 안에 들어가야 한다.

    들어가지 않으면 절단을 고치려던 재시도가 컨텍스트 초과를 새로 만든다.
    """
    from app.services.summary.settings import (
        GROUP_MAX_CHARS,
        LOCAL_NUM_CTX,
        SUMMARY_MAP_RETRY_MAX_TOKENS,
    )

    estimated = GROUP_MAX_CHARS * 0.79 + 150 + SUMMARY_MAP_RETRY_MAX_TOKENS
    assert estimated < LOCAL_NUM_CTX, f"추정 {estimated:.0f}토큰 > num_ctx {LOCAL_NUM_CTX}"
    assert SUMMARY_MAP_RETRY_MAX_TOKENS > SUMMARY_MAP_MAX_TOKENS


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


class TestMapOutputBudget:
    """출력 상한에 걸려 잘린 JSON은 파싱조차 안 되므로 그대로 두면 영구 실패한다.

    실기기 로그: 908노드 중 225번째가 3회 연속 finish_length로 실패해, 이미 성공한
    675개 노드가 있는데도 매번 문서 전체가 버려졌다(temperature 0이라 재시도해도 동일).
    """

    @staticmethod
    def _sequence(responses: list[dict]):
        """호출마다 다른 응답을 돌려주는 캡처(응답이 떨어지면 마지막 것을 반복)."""

        class _Seq(_Capture):
            def __call__(self, url, payload, api_key):
                self.calls.append((url, payload, api_key))
                self.payloads.append(payload)
                index = min(len(self.calls) - 1, len(responses) - 1)
                return responses[index]

        return _Seq({})

    def test_truncated_output_is_retried_with_a_larger_budget(self):
        from app.services.summary.settings import SUMMARY_MAP_RETRY_MAX_TOKENS

        capture = self._sequence(
            [
                _chat_response("{\"summary\":\"잘린", finish_reason="length"),
                _chat_response(json.dumps({"summary": "정상 요약"})),
            ]
        )
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=capture,
        )

        result = provider.summarize_group(_group_request())

        assert result.summary_text == "정상 요약"
        assert len(capture.calls) == 2, "예산을 늘려 다시 부르지 않았다"
        assert capture.calls[0][1]["max_tokens"] == SUMMARY_MAP_MAX_TOKENS
        assert capture.calls[1][1]["max_tokens"] == SUMMARY_MAP_RETRY_MAX_TOKENS

    def test_still_truncated_after_retry_becomes_a_splittable_reason(self):
        """예산을 늘려도 넘치면 남은 수단은 입력을 줄이는 것뿐이다."""
        capture = self._sequence(
            [_chat_response("{\"summary\":\"잘린", finish_reason="length")]
        )
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=capture,
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "map_finish_length"
        assert len(capture.calls) == 2, "재시도는 한 번뿐이어야 한다(호출 증폭 방지)"

    def test_other_failures_are_not_retried(self):
        """출력 절단이 아닌 실패까지 다시 부르면 호출만 낭비한다."""
        capture = self._sequence([_chat_response("{}", finish_reason="content_filter")])
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=capture,
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "finish_content_filter"
        assert len(capture.calls) == 1


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


class TestEmptySummaryGate:
    """게이트가 저장 계층과 어긋나면 artifact 0개인 요약이 SUCCEEDED로 저장된다."""

    def test_items_without_sources_do_not_count_as_content(self):
        """build_artifacts는 출처 없는 항목을 버린다 — 게이트도 같은 기준이어야 한다."""
        content = json.dumps(
            {"keyConcepts": [{"term": "심부전", "explanation": "설명"}]}  # sourceGroupIds 없음
        )
        provider, _ = _provider(_chat_response(content))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(
                DocumentRequest(
                    group_summaries=[GroupSummary("g0", "구역", "요약", ["c0"])],
                    learner_level="nursing_student",
                    language="ko",
                )
            )

        assert caught.value.reason == "reduce_empty_summary"

    def test_items_without_text_do_not_count_as_content(self):
        content = json.dumps({"keyConcepts": [{"term": "  ", "sourceGroupIds": ["g0"]}]})
        provider, _ = _provider(_chat_response(content))

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_document(
                DocumentRequest(
                    group_summaries=[GroupSummary("g0", "구역", "요약", ["c0"])],
                    learner_level="nursing_student",
                    language="ko",
                )
            )

        assert caught.value.reason == "reduce_empty_summary"


def test_discarded_subtrees_do_not_fail_the_whole_document():
    """저장조차 하지 않는 하위 트리의 형식 위반으로 문서 전체를 죽이면 안 된다.

    사용자가 끈 sections를 모델이 굳이 채워 보내고 그 안의 sourceGroupIds 하나가
    배열이 아니면, 멀쩡한 overview가 있는데도 문서가 영구 실패했다.
    """
    content = json.dumps(
        {
            "overview": {"text": "개요", "sourceGroupIds": ["g0"]},
            "sections": [{"title": "구역", "summary": "내용", "sourceGroupIds": "g0"}],
        }
    )
    provider, _ = _provider(_chat_response(content))

    result = provider.summarize_document(
        DocumentRequest(
            group_summaries=[GroupSummary("g0", "구역", "요약", ["c0"])],
            learner_level="nursing_student",
            language="ko",
            include_sections=False,
        )
    )

    assert result["overview"]["sourceChunkIds"] == ["c0"]
    assert "sections" not in result


def test_model_error_without_overflow_signs_is_retried_with_a_strict_schema():
    """스키마가 열어 준 error 탈출구를 모델이 고르면 그 청크는 영구히 요약되지 않는다.

    error 대안은 잘린 프롬프트를 감지하려고 둔 것이다 — 초과 징후가 없으면 오류 형태를
    뺀 문법으로 한 번 더 물어본다.
    """
    # native 경로만 스키마를 실제로 보낸다 — 응답도 native 형태여야 한다.
    def native(content: str) -> dict:
        return {"message": {"content": content}, "done_reason": "stop"}

    responses = [
        native(json.dumps({"error": "내용을 요약할 수 없습니다"})),
        native(json.dumps({"summary": "정상 요약"})),
    ]

    class _Seq(_Capture):
        def __call__(self, url, payload, api_key):
            self.calls.append((url, payload, api_key))
            self.payloads.append(payload)
            return responses[min(len(self.calls) - 1, len(responses) - 1)]

    capture = _Seq({})
    provider = OpenAICompatibleSummaryProvider(
        endpoint="http://127.0.0.1:11434/v1",
        model_name="qwen3:8b",
        api_key="",
        is_local=True,
        http_client=capture,
    )

    result = provider.summarize_group(_group_request())

    assert result.summary_text == "정상 요약"
    assert len(capture.calls) == 2
    # 두 번째 호출은 error 형태가 빠진 엄격한 문법이어야 한다
    assert "anyOf" not in capture.calls[1][1]["format"]
    assert capture.calls[1][1]["format"]["required"] == ["summary"]


def test_context_overflow_hints_are_split_per_path():
    """경로마다 표지가 다르다 — 합치면 한쪽에서 오분류가 난다."""
    from app.services.summary.endpoint import (
        HTTP_CONTEXT_OVERFLOW_HINTS,
        MODEL_CONTEXT_OVERFLOW_HINTS,
    )

    # HTTP 400 본문에는 컨텍스트를 명시적으로 가리키는 것만 인정한다. 'too long'은
    # 컨텍스트와 무관한 400(string_above_max_length 등)에도 흔해 분할을 낭비시킨다.
    assert "too long" not in HTTP_CONTEXT_OVERFLOW_HINTS
    assert "context_length_exceeded" in HTTP_CONTEXT_OVERFLOW_HINTS
    # 모델 자연어 응답은 한국어로도 온다(한국어로 답하라고 지시했다).
    assert "너무 깁니다" in MODEL_CONTEXT_OVERFLOW_HINTS


def test_korean_overflow_message_triggers_adaptation():
    from app.services.summary.provider import _looks_like_context_overflow

    assert _looks_like_context_overflow({"error": "입력이 너무 깁니다. 요약할 수 없습니다."})
    assert not _looks_like_context_overflow({"error": "이 내용은 요약하지 않겠습니다."})


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


class TestProviderCheckpointFingerprint:
    def _configured(
        self,
        *,
        endpoint: str = "https://api.example.com/v1",
        api_key: str = "secret-a",
        is_local: bool = False,
        model_digest: str | None = None,
        model_name: str = "same-model-name",
        provider_identity_digest: str | None = None,
        provider_identity_generation: str | None = None,
    ) -> OpenAICompatibleSummaryProvider:
        return OpenAICompatibleSummaryProvider(
            endpoint=endpoint,
            model_name=model_name,
            api_key=api_key,
            is_local=is_local,
            http_client=lambda *_: {},
            model_digest=model_digest,
            provider_identity_digest=provider_identity_digest,
            provider_identity_generation=provider_identity_generation,
        )

    def test_endpoint_is_hashed_and_changes_checkpoint_identity(self):
        first = self._configured(endpoint="https://api-a.example.com/v1")
        second = self._configured(endpoint="https://api-b.example.com/v1")

        first_fingerprint = provider_checkpoint_fingerprint(first)

        assert len(first_fingerprint) == 64
        assert first_fingerprint != provider_checkpoint_fingerprint(second)

    def test_equivalent_endpoint_spellings_have_the_same_identity(self):
        upper = self._configured(endpoint="https://API.Example.com/v1/")
        normalized = self._configured(endpoint="https://api.example.com/v1")

        assert provider_checkpoint_fingerprint(upper) == provider_checkpoint_fingerprint(normalized)

    def test_api_key_is_not_part_of_persisted_fingerprint_but_changes_runtime_identity(self):
        first = self._configured(api_key="secret-a")
        rotated = self._configured(api_key="secret-b")

        assert provider_checkpoint_fingerprint(first) == provider_checkpoint_fingerprint(rotated)
        assert providers_share_runtime_configuration(first, rotated) is False

    def test_non_secret_credential_identity_breaks_checkpoint_reuse(self):
        first = self._configured(provider_identity_digest="identity-a")
        rotated = self._configured(provider_identity_digest="identity-b")

        assert provider_checkpoint_fingerprint(first) != provider_checkpoint_fingerprint(rotated)

    def test_deployment_or_local_model_tag_breaks_checkpoint_reuse(self):
        first = self._configured(model_name="deployment-a")
        changed = self._configured(model_name="deployment-b")

        assert provider_checkpoint_fingerprint(first) != provider_checkpoint_fingerprint(changed)

    def test_optional_model_digest_breaks_reuse(self):
        first = self._configured(model_digest="sha256:digest-a")
        replaced = self._configured(model_digest="sha256:digest-b")

        assert provider_checkpoint_fingerprint(first) != provider_checkpoint_fingerprint(replaced)

    def test_missing_digest_fail_safe_generation_breaks_reuse(self):
        first = self._configured(is_local=True, provider_identity_generation="generation-a")
        next_run = self._configured(is_local=True, provider_identity_generation="generation-b")

        assert provider_checkpoint_fingerprint(first) != provider_checkpoint_fingerprint(next_run)

    def test_generation_config_breaks_reuse(self, monkeypatch):
        from app.services.summary import provider as provider_mod

        configured = self._configured()
        before = provider_checkpoint_fingerprint(configured)
        monkeypatch.setattr(
            provider_mod, "SUMMARY_MAP_MAX_TOKENS", provider_mod.SUMMARY_MAP_MAX_TOKENS + 1
        )

        assert provider_checkpoint_fingerprint(configured) != before


def test_internal_budget_retry_rechecks_guard_before_second_http_request():
    """공개 summarize 호출 안의 재요청도 동의 가드를 건너뛰지 않는다."""
    responses = [
        _chat_response("{", finish_reason="length"),
        _chat_response('{"summary":"두 번째 응답"}'),
    ]
    transport_calls = 0

    def transport(*_args):
        nonlocal transport_calls
        response = responses[transport_calls]
        transport_calls += 1
        return response

    provider = OpenAICompatibleSummaryProvider(
        endpoint="https://api.example.com/v1",
        model_name="test-model",
        api_key="secret",
        is_local=False,
        http_client=transport,
    )
    guard_calls = 0

    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise SummaryConsentRevoked

    provider.set_before_request_guard(guard)

    with pytest.raises(SummaryConsentRevoked):
        provider.summarize_group(_group_request())

    assert guard_calls == 2
    assert transport_calls == 1, "동의 철회 뒤 두 번째 요청을 보내면 안 된다"


class TestActualRequestRetryBudget:
    def test_production_node_budget_has_fixed_request_and_deadline_ceiling(self):
        budget = new_provider_request_budget(clock=lambda: 0.0)

        assert budget.request_limit == 6
        assert budget.total_deadline_seconds == 360.0
        for _ in range(6):
            assert budget.claim_request(300.0) == 300.0
        with pytest.raises(SummaryNetworkError) as caught:
            budget.claim_request(300.0)
        assert caught.value.reason == "node_request_budget_exhausted"

    def test_failed_pre_request_guard_does_not_consume_http_budget(self):
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=lambda *_: pytest.fail("guard failure must stop before transport"),
        )

        def revoked() -> None:
            raise SummaryConsentRevoked

        provider.set_before_request_guard(revoked)
        budget = ProviderRequestBudget(request_limit=2, total_deadline_seconds=60)

        with provider_request_budget_scope(budget), pytest.raises(SummaryConsentRevoked):
            provider.summarize_group(_group_request())

        assert budget.requests_started == 0

    def test_guard_time_is_part_of_node_deadline(self):
        now = [0.0]
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=lambda *_: pytest.fail("expired guard must stop before transport"),
        )
        provider.set_before_request_guard(lambda: now.__setitem__(0, 6.0))
        budget = ProviderRequestBudget(
            request_limit=2,
            total_deadline_seconds=5.0,
            clock=lambda: now[0],
        )

        with provider_request_budget_scope(budget), pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.reason == "node_deadline_exceeded"
        assert budget.requests_started == 0

    @pytest.mark.parametrize(
        "category", ["timeout", "connect_failed", "rate_limited", "server_error"]
    )
    def test_only_transient_actual_requests_are_retried(self, category, monkeypatch):
        calls = 0
        sleeps: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", sleeps.append)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 0.0)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SummaryNetworkError(category, "test_transient")
            return _chat_response('{"summary":"복구됨"}')

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        assert provider.summarize_group(_group_request()).summary_text == "복구됨"
        assert calls == 2
        assert sleeps == [0.5]

    @pytest.mark.parametrize("category", ["auth_failed", "bad_response"])
    def test_auth_and_schema_errors_are_not_retried(self, category, monkeypatch):
        calls = 0

        def must_not_sleep(_delay: float) -> None:
            pytest.fail("영구 오류에는 backoff가 없어야 한다")

        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", must_not_sleep)

        def transport(*_args):
            nonlocal calls
            calls += 1
            raise SummaryNetworkError(category, "permanent")

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        with pytest.raises(SummaryNetworkError):
            provider.summarize_group(_group_request())
        assert calls == 1

    def test_method_retry_does_not_multiply_internal_budget_requests(self, monkeypatch):
        """finish-length 뒤 5xx여도 공개 메서드를 3회 되감아 12회 호출하지 않는다."""
        calls = 0
        sleeps: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", sleeps.append)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 0.0)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _chat_response("{", finish_reason="length")
            raise SummaryNetworkError("server_error", "http_503")

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        with pytest.raises(SummaryNetworkError) as caught:
            provider.summarize_group(_group_request())

        assert caught.value.category == "server_error"
        assert calls == 4  # 출력 예산 1회 + 동일 두 번째 payload 최대 3회
        assert sleeps == [0.5, 1.0]

    def test_shared_node_request_budget_caps_internal_and_network_retries(self, monkeypatch):
        calls = 0
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", lambda _delay: None)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 0.0)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _chat_response("{", finish_reason="length")
            raise SummaryNetworkError("server_error", "http_503")

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )
        budget = ProviderRequestBudget(request_limit=2, total_deadline_seconds=60)

        with provider_request_budget_scope(budget), pytest.raises(SummaryNetworkError):
            provider.summarize_group(_group_request())

        assert calls == 2
        assert budget.requests_started == 2

    def test_remaining_node_deadline_caps_each_actual_http_timeout(self, monkeypatch):
        now = [0.0]
        timeouts: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", lambda _delay: None)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 0.0)

        def post_json(*_args, timeout: float, **_kwargs):
            timeouts.append(timeout)
            now[0] += 3.0
            raise SummaryNetworkError("server_error", "http_503")

        monkeypatch.setattr(endpoint_mod, "post_json", post_json)
        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
        )
        budget = ProviderRequestBudget(
            request_limit=6,
            total_deadline_seconds=5.0,
            clock=lambda: now[0],
        )

        with provider_request_budget_scope(budget), pytest.raises(SummaryNetworkError):
            provider.summarize_group(_group_request())

        assert timeouts == [5.0, 2.0]
        assert budget.requests_started == 2

    def test_retry_after_is_honored_but_capped(self, monkeypatch):
        calls = 0
        sleeps: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", sleeps.append)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SummaryNetworkError(
                    "rate_limited", "http_429", retry_after_seconds=120.0
                )
            return _chat_response('{"summary":"복구됨"}')

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        assert provider.summarize_group(_group_request()).summary_text == "복구됨"
        assert sleeps == [10.0]

    def test_non_finite_retry_after_falls_back_to_bounded_backoff(self, monkeypatch):
        calls = 0
        sleeps: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", sleeps.append)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 0.0)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SummaryNetworkError(
                    "rate_limited", "http_429", retry_after_seconds=float("nan")
                )
            return _chat_response('{"summary":"복구됨"}')

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        assert provider.summarize_group(_group_request()).summary_text == "복구됨"
        assert sleeps == [0.5]

    def test_jittered_backoff_is_capped(self, monkeypatch):
        calls = 0
        sleeps: list[float] = []
        monkeypatch.setattr(provider_mod, "_sleep_before_request_retry", sleeps.append)
        monkeypatch.setattr(provider_mod, "_retry_jitter", lambda: 999.0)

        def transport(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SummaryNetworkError("connect_failed", "test")
            return _chat_response('{"summary":"복구됨"}')

        provider = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="test-model",
            api_key="secret",
            is_local=False,
            http_client=transport,
        )

        provider.summarize_group(_group_request())
        assert sleeps == [5.0]
