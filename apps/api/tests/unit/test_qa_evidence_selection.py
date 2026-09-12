"""평가 전용 원문 선택기는 모델 산문을 근거로 승격하지 않는다."""

import copy
import json
import uuid

import pytest

from app.qa_eval.evidence_selection import EvidenceSelectionError, select_evidence
from app.services.qa.provider import QaContextChunk, QaHistoryTurn, QaRequest
from app.services.qa.settings import CONTEXT_MAX_CHUNKS, STREAM_TOTAL_DEADLINE_SEC
from app.services.summary.cancellation import (
    SummaryCancellationSignal,
    SummaryCancelled,
    summary_cancellation_scope,
)
from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.settings import LOCAL_MAX_TOKENS, LOCAL_NUM_CTX
from tests.unit.test_qa_schema import _lookup


def _input():
    lookup = _lookup(("voltage", "전압에 따른 분류: 24V형, 48V형."),
                     ("power", "출력에 따른 분류: 8W형, 16W형."))
    request = QaRequest(question="장치의 분류", chunks=[
        QaContextChunk(cid, "private-section", ref.text, 987, 988)
        for cid, ref in lookup.items()
    ], history=[QaHistoryTurn("user", "이전 질문은 근거가 아니다.")])
    return request, lookup


def _output(indices=(0, 1), status="selected", order=(0, 1)):
    return {"status": status, "decisions": {str(i): i in indices for i in order}}


def _client(output, calls, **envelope):
    def send(url, payload, key):
        calls.append((url, copy.deepcopy(payload), key))
        return {"message": {"content": json.dumps(output, ensure_ascii=False)},
                "done": True, "done_reason": "stop", **envelope}
    return send


def test_selection_copies_whole_visible_sources_not_model_prose():
    request, lookup = _input()
    before = copy.deepcopy((request, lookup))
    calls = []
    result = select_evidence(request, lookup, http_client=_client(
        _output(order=(1, 0)), calls,
    ))
    assert result.status == "selected"
    # 모델이 고른 순서로 원문을 이어 붙여 새 관계를 만들지 않는다.
    assert [e.chunk_id for e in result.excerpts] == ["voltage", "power"]
    for excerpt, chunk in zip(result.excerpts, request.chunks, strict=True):
        assert excerpt.text == chunk.text
        assert excerpt.content_hash == lookup[chunk.chunk_id].content_hash
        assert excerpt.source_refs == lookup[chunk.chunk_id].source_refs
        assert excerpt.start == 0 and excerpt.end == len(chunk.text)
        assert not excerpt.input_truncated
    assert (request, lookup) == before
    result.excerpts[0].source_refs[0]["bbox"][0] = -1
    assert (request, lookup) == before
    assert len(calls) == 1
    url, payload, key = calls[0]
    assert url == "http://127.0.0.1:11434/api/chat" and key == ""
    assert payload["stream"] is False and payload["think"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": LOCAL_NUM_CTX,
                                  "num_predict": LOCAL_MAX_TOKENS}
    user = json.loads(payload["messages"][1]["content"])
    assert user["chunks"] == [{"chunkIndex": i, "text": c.text}
                              for i, c in enumerate(request.chunks)]
    assert "private-section" not in payload["messages"][1]["content"]
    assert "987" not in payload["messages"][1]["content"]
    assert "근거 아님" in payload["messages"][0]["content"]
    decisions_schema = payload["format"]["properties"]["decisions"]
    assert decisions_schema["required"] == ["0", "1"]
    assert decisions_schema["properties"] == {"0": {"type": "boolean"}, "1": {"type": "boolean"}}


@pytest.mark.parametrize("output", [
    None, [], "text",
    {"status": "selected", "decisions": []},
    {"status": "selected", "decisions": "voltage"},
    _output(order=(0,)),  # 입력 하나에 대한 판단을 빠뜨림
    _output(order=(0, 2)),  # 보이지 않은 청크
    _output(order=(0, 0)),  # 같은 청크를 두 번 판단
    _output(order=(0, True)),
    _output(order=(0, 1.0)),
    _output(order=(0, "01")),
    {"status": "selected", "decisions": {"0": 1, "1": False}},
    {"status": "selected", "decisions": [None, True]},
    {"status": "selected", "decisions": {"0": {"include": True, "text": "96V"}, "1": False}},
    {**_output(), "text": "전압은 96V이다."},
    _output(order=tuple(range(CONTEXT_MAX_CHUNKS + 1))),
    _output(status="answered"),
    _output(status="not_found"),
    _output(()),
])
def test_bad_selection_fails_closed_without_id_repair(output):
    request, lookup = _input()
    calls = []
    with pytest.raises(EvidenceSelectionError):
        select_evidence(request, lookup, http_client=_client(output, calls))
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["not_found", "insufficient_evidence", "conflicting_evidence"])
def test_abstention_and_conflict_are_not_promoted(status):
    request, lookup = _input()
    ids = ["voltage", "power"] if status == "conflicting_evidence" else []
    result = select_evidence(request, lookup, http_client=_client(
        _output((0, 1) if ids else (), status), [],
    ))
    assert result.status == status
    assert len(result.excerpts) == len(ids)


def test_visible_prefix_is_not_expanded_to_unseen_source_tail():
    request, lookup = _input()
    lookup["voltage"].text += " 모델에게 보이지 않은 후속 문장."
    result = select_evidence(request, lookup, http_client=_client(
        _output((0,)), [],
    ))
    assert result.excerpts[0].input_truncated
    assert result.excerpts[0].text == request.chunks[0].text
    assert "후속 문장" not in result.excerpts[0].text


@pytest.mark.parametrize("mutation", ["unknown", "text", "duplicate", "ref_id", "large",
                                      "history", "question", "not_classification"])
def test_invalid_input_never_calls_model(mutation):
    request, lookup = _input()
    if mutation == "unknown":
        del lookup["voltage"]
    elif mutation == "text":
        request.chunks[0].text = "다른 청크에 있는 8W를 전압으로 바꾼다."
    elif mutation == "duplicate":
        request.chunks.append(request.chunks[0])
    elif mutation == "ref_id":
        lookup["voltage"].chunk_id = "other-document"
    elif mutation == "large":
        request.chunks[0].text = "가" * 3001
    elif mutation == "history":
        request.history[0].content = "가" * 2001
    elif mutation == "question":
        request.question = "분류 " + "가" * 2001
    else:
        request.question = "장치의 정의"
    calls = []
    with pytest.raises(EvidenceSelectionError):
        select_evidence(request, lookup, http_client=_client({}, calls))
    assert calls == []


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"),
                                    STREAM_TOTAL_DEADLINE_SEC + 1])
def test_deadline_cannot_be_extended_or_disabled(seconds):
    request, lookup = _input()
    calls = []
    with pytest.raises(EvidenceSelectionError):
        select_evidence(request, lookup, deadline_seconds=seconds,
                        http_client=_client({}, calls))
    assert not calls


def test_cancelled_selection_does_not_call_model():
    request, lookup = _input()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    signal.cancel()
    calls = []
    with pytest.raises(SummaryCancelled):
        select_evidence(request, lookup, cancellation_signal=signal,
                        http_client=_client({}, calls))
    assert not calls


def test_transport_failure_is_not_retried_or_downgraded():
    request, lookup = _input()
    calls = []

    def fail(*args):
        calls.append(args)
        raise SummaryNetworkError("server_error")

    with pytest.raises(SummaryNetworkError):
        select_evidence(request, lookup, http_client=fail)
    assert len(calls) == 1


@pytest.mark.parametrize("envelope", [{"done_reason": "length"}, {"done": False},
                                     {"error": "private native error text"},
                                     {"prompt_eval_count": LOCAL_NUM_CTX}])
def test_truncated_native_result_is_not_selected(envelope):
    request, lookup = _input()
    with pytest.raises((EvidenceSelectionError, SummaryNetworkError)):
        select_evidence(request, lookup, http_client=_client(
            _output((0,)), [], **envelope,
        ))


@pytest.mark.parametrize("raw", [
    '{"status":"selected","status":"not_found","decisions":[]}',
    '{"status":"selected","decisions":{"0":true,"0":false,"1":true}}',
    '```json\\n{"status":"not_found","decisions":[]}\\n```',
    '{"status":"not_found","decisions":[]} unexpected trailing prose',
    '{"status":"selected","decisions":[',
])
def test_malformed_output_is_not_partially_recovered(raw):
    request, lookup = _input()

    def send(*args):
        return {"done": True, "done_reason": "stop", "message": {"content": raw}}

    with pytest.raises(EvidenceSelectionError):
        select_evidence(request, lookup, http_client=send)


def test_hidden_lookup_chunk_is_not_selectable():
    request, lookup = _input()
    request.chunks.pop()
    with pytest.raises(EvidenceSelectionError, match="invalid_output_sources"):
        select_evidence(request, lookup, http_client=_client(
            _output((1,)), [],
        ))


def test_empty_context_does_not_call_model():
    request = QaRequest(question="장치의 분류", chunks=[])
    calls = []
    result = select_evidence(request, {}, http_client=_client({}, calls))
    assert result.status == "not_found" and not result.excerpts and not calls


def test_large_excerpt_is_not_truncated_to_the_claim_limit():
    request, lookup = _input()
    literal = "기준이 있는 원문. " * 100 + "그러나 이 경우에는 적용하지 않는다."
    request.chunks[0].text = lookup["voltage"].text = literal
    result = select_evidence(request, lookup, http_client=_client(
        _output((0,)), [],
    ))
    assert result.excerpts[0].text == literal
    assert result.excerpts[0].end == len(literal) > 600


def test_snapshot_change_during_model_call_discards_selection():
    request, lookup = _input()

    def send(*args):
        lookup["voltage"].source_refs[0]["bbox"][0] = -1
        return _client(_output((0,)), [])(*args)

    with pytest.raises(EvidenceSelectionError, match="input_changed"):
        select_evidence(request, lookup, http_client=send)


@pytest.mark.parametrize("inherit", [False, True])
def test_inflight_and_inherited_cancellation_discards_selection(inherit):
    request, lookup = _input()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())

    def send(*args):
        signal.cancel()
        return _client(_output((0,)), [])(*args)

    with summary_cancellation_scope(signal if inherit else None), pytest.raises(SummaryCancelled):
        select_evidence(request, lookup, cancellation_signal=None if inherit else signal,
                        http_client=send)


def test_deadline_after_completed_transport_discards_selection(monkeypatch):
    from app.qa_eval.evidence_selection import ProviderRequestBudget

    request, lookup = _input()

    def send(*args):
        monkeypatch.setattr(ProviderRequestBudget, "remaining_seconds", lambda self: 0)
        return _client(_output((0,)), [])(*args)

    with pytest.raises(SummaryNetworkError, match="timeout"):
        select_evidence(request, lookup, http_client=send)


def test_unapproved_model_does_not_call_or_download():
    request, lookup = _input()
    calls = []
    with pytest.raises(EvidenceSelectionError):
        select_evidence(request, lookup, model="other-model", http_client=_client({}, calls))
    assert not calls


def test_transport_keeps_deadline_and_cancellation_on_safe_http_path(monkeypatch):
    from app.services.summary import endpoint

    request, lookup = _input()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls = []

    def post(url, payload, key, **kwargs):
        assert kwargs["is_local"] is True and 0 < kwargs["timeout"] <= 12
        assert kwargs["cancellation_signal"] is signal
        assert kwargs["max_response_bytes"] > 0
        return _client(_output((), "not_found"), calls)(url, payload, key)

    monkeypatch.setattr(endpoint, "post_json", post)
    assert select_evidence(request, lookup, deadline_seconds=12,
                           cancellation_signal=signal).status == "not_found"
    assert len(calls) == 1
