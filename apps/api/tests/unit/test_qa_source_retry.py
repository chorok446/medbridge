"""출처 필드 누락은 한 번만 재요청하고 서버가 ID를 만들어 붙이지 않는다."""

import copy
import json

import pytest

from app.services.qa import streaming
from app.services.qa.provider import QaContextChunk, QaRequest
from app.services.qa.schema import classify_claim_event
from app.services.qa.stream_service import _final_status
from app.services.summary.endpoint import SummaryNetworkError
from tests.unit.test_qa_schema import _lookup

TEXT = "예제 장치는 두 가지 방식으로 분류된다."
REQUEST = QaRequest(question="예제 장치의 분류", chunks=[
    QaContextChunk("c1", None, TEXT, 1, 1),
])


class Token:
    cancelled = False

    def is_cancelled(self):
        return self.cancelled


def response(claim, hint="answered"):
    content = "\n".join(json.dumps(e, ensure_ascii=False) for e in [
        claim, {"type": "final", "answerStatus": hint},
    ])
    return iter([json.dumps({"message": {"content": content}, "done": True})])


def provider(source, *, local=True):
    return streaming.OpenAICompatibleStreamingQaProvider(
        endpoint="http://127.0.0.1:11434/v1" if local else "https://example.test/v1",
        model_name="qwen3:8b", api_key="" if local else "test", is_local=local,
        line_source=source,
    )


GOOD = {"type": "claim", "text": TEXT, "sourceChunkIds": ["c1"]}


@pytest.mark.parametrize("fields", [{}, {"sourceChunkIds": None}, {"sourceChunkIds": []},
                                    {"sourceChunkIds": "c1"}, {"sourceChunkIds": [""]}])
def test_missing_source_retries_once_without_exposing_failed_attempt(fields):
    calls = []
    bad = {"type": "claim", "text": TEXT, **fields}

    def source(_url, payload, _key):
        calls.append(copy.deepcopy(payload))
        return response(bad if len(calls) == 1 else GOOD)

    events = list(provider(source).stream_answer(REQUEST, Token()))
    assert len(calls) == 2
    assert events == [GOOD, {"type": "final", "answerStatus": "answered"}]
    assert calls[0]["messages"][1:] == calls[1]["messages"][1:]
    assert calls[1]["messages"][0]["content"].startswith(calls[0]["messages"][0]["content"])
    assert calls[1]["messages"][0]["content"] != calls[0]["messages"][0]["content"]
    assert calls[0]["options"] == calls[1]["options"]


def test_repeated_omission_stays_unverified_without_third_generation():
    calls = []
    bad = {"type": "claim", "text": TEXT}

    def source(*_):
        calls.append(1)
        return response(bad)

    events = list(provider(source).stream_answer(REQUEST, Token()))
    assert len(calls) == 2
    claim, reason = classify_claim_event(events[0], _lookup(("c1", TEXT)), claim_index=0)
    assert claim is None and reason == "no_valid_source"
    assert _final_status([], events[-1]["answerStatus"], had_results=True)[0].value == (
        "insufficient_evidence"
    )


@pytest.mark.parametrize("hint", ["not_found", "insufficient_evidence"])
def test_abstention_does_not_trigger_source_retry(hint):
    calls = []

    def source(*_):
        calls.append(1)
        return response({"type": "claim", "text": TEXT}, hint)

    assert list(provider(source).stream_answer(REQUEST, Token()))[-1]["answerStatus"] == hint
    assert len(calls) == 1


def test_unknown_source_is_not_repaired_or_retried():
    calls = []

    def source(*_):
        calls.append(1)
        return response({**GOOD, "sourceChunkIds": ["unknown"]})

    events = list(provider(source).stream_answer(REQUEST, Token()))
    assert len(calls) == 1
    assert events[0]["sourceChunkIds"] == ["unknown"]
    _, reason = classify_claim_event(events[0], _lookup(("c1", TEXT)), claim_index=0)
    assert reason == "no_valid_source"


def test_source_retry_shares_network_attempt_budget(monkeypatch):
    monkeypatch.setattr(streaming, "_wait_for_ollama_retry", lambda *_: True)
    calls = []

    def source(*_):
        calls.append(1)
        if len(calls) == 1:
            raise SummaryNetworkError("server_error", "http_500")
        return response({"type": "claim", "text": TEXT} if len(calls) == 2 else GOOD)

    events = list(provider(source).stream_answer(REQUEST, Token()))
    assert len(calls) == 3
    assert events[0] == GOOD


def test_external_provider_does_not_make_additional_request():
    calls = []

    def source(*_):
        calls.append(1)
        return response({"type": "claim", "text": TEXT})

    list(provider(source, local=False).stream_answer(REQUEST, Token()))
    assert len(calls) == 1


def test_missing_sources_do_not_extend_expired_deadline(monkeypatch):
    clock = iter([0, 0, 601])
    monkeypatch.setattr(streaming.time, "monotonic", lambda: next(clock))
    calls = []

    def source(*_):
        calls.append(1)
        return response({"type": "claim", "text": TEXT})

    with pytest.raises(SummaryNetworkError) as error:
        list(provider(source).stream_answer(REQUEST, Token()))
    assert error.value.category == "timeout"
    assert len(calls) == 1


def test_missing_sources_do_not_exceed_remaining_attempts(monkeypatch):
    monkeypatch.setattr(streaming, "STREAM_OLLAMA_MAX_ATTEMPTS", 1)
    calls = []

    def source(*_):
        calls.append(1)
        return response({"type": "claim", "text": TEXT})

    events = list(provider(source).stream_answer(REQUEST, Token()))
    assert "sourceChunkIds" not in events[0]
    assert len(calls) == 1


def test_mixed_valid_and_missing_sources_do_not_retry_whole_answer():
    assert not streaming._missing_claim_sources([GOOD, {"type": "claim", "text": TEXT}])
    assert not streaming._missing_claim_sources([])
