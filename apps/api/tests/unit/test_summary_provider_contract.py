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
from app.services.summary.settings import SUMMARY_MAP_MAX_TOKENS
from app.services.tasks.summary_job import _classify_pipeline_failure


class _Capture:
    def __init__(self, response: dict):
        self.response = response
        self.payloads: list[dict] = []

    def __call__(self, _url, payload, _api_key):
        self.payloads.append(payload)
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
