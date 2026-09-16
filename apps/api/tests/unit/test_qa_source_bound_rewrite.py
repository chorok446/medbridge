"""출처 고정 재서술 시험: 다른 원문으로 근거를 보충하지 않고 검토용 후보만 반환한다."""

import copy
import json
import uuid

import pytest

from app.qa_eval.source_bound_rewrite import SourceBoundRewriteError, rewrite_source
from app.services.qa.provider import QaContextChunk, QaHistoryTurn, QaRequest
from app.services.qa.schema import REJECT_NUMBER_ABSENT, classify_claim_event
from app.services.qa.settings import CLAIM_TEXT_MAX_CHARS, MAX_CLAIMS, STREAM_TOTAL_DEADLINE_SEC
from app.services.summary.cancellation import (
    SummaryCancellationSignal,
    SummaryCancelled,
    summary_cancellation_scope,
)
from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.settings import LOCAL_MAX_TOKENS, LOCAL_NUM_CTX
from tests.unit.test_qa_literal_rebinding import OTHER, TEXT
from tests.unit.test_qa_schema import _lookup

REWRITE = TEXT.replace("기준은", "기준값은")


def inputs(source=TEXT):
    lookup = _lookup(("voltage-private-id", source), ("power-private-id", OTHER))
    return QaRequest(question="예제 장치의 분류", chunks=[
        QaContextChunk("voltage-private-id", "private-title", source, 987, 988),
    ]), lookup


def response(output, calls, **envelope):
    def send(url, payload, key):
        calls.append((url, copy.deepcopy(payload), key))
        return {"message": {"content": json.dumps(output, ensure_ascii=False)},
                "done": True, "done_reason": "stop", **envelope}
    return send


def output(*texts, status="rewritten"):
    return {"status": status, "texts": list(texts)}


def test_reproduce_wrong_citation_then_bind_the_same_rewrite_to_its_only_source():
    request, lookup = inputs()
    before = copy.deepcopy((request, lookup))
    rejected, reason = classify_claim_event(
        {"text": REWRITE, "sourceChunkIds": ["power-private-id"]}, lookup, claim_index=0,
    )
    assert rejected is None and reason == REJECT_NUMBER_ABSENT
    calls = []
    result = rewrite_source(request, lookup, http_client=response(output(REWRITE), calls))
    assert result.model_status == "rewritten"  # 최종 completed/supported 상태가 아니다.
    assert result.candidate_texts == (REWRITE,)
    assert result.rejection_reasons == ()
    assert result.evidence.chunk_id == request.chunks[0].chunk_id
    assert result.evidence.text == TEXT
    assert result.evidence.content_hash == lookup[result.evidence.chunk_id].content_hash
    assert result.evidence.source_refs == lookup[result.evidence.chunk_id].source_refs
    assert (result.evidence.start, result.evidence.end) == (0, len(TEXT))
    assert not result.evidence.input_truncated
    assert (request, lookup) == before
    result.evidence.source_refs[0]["bbox"][0] = -1
    assert (request, lookup) == before
    assert len(calls) == 1
    url, payload, key = calls[0]
    assert url == "http://127.0.0.1:11434/api/chat" and key == ""
    user = json.loads(payload["messages"][1]["content"])
    assert user == {"question": request.question, "sourceText": TEXT}
    assert OTHER not in json.dumps(payload, ensure_ascii=False)
    assert "private" not in json.dumps(payload) and "987" not in json.dumps(payload)
    assert payload["stream"] is False and payload["think"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": LOCAL_NUM_CTX,
                                  "num_predict": LOCAL_MAX_TOKENS}
    assert "maxLength" not in json.dumps(payload["format"])
    assert "로마 숫자·아라비아 숫자·문자 등급" in payload["messages"][0]["content"]


def test_other_lookup_source_and_hidden_numbers_cannot_support_the_rewrite():
    request, lookup = inputs()
    result = rewrite_source(request, lookup, http_client=response(output(OTHER), []))
    assert result.candidate_texts == ()
    assert result.rejection_reasons == (REJECT_NUMBER_ABSENT,)
    assert result.evidence.chunk_id == "voltage-private-id"


@pytest.mark.parametrize(("roman", "arabic"), [("I", "1"), ("Ⅱ", "2"), ("IV", "4")])
def test_grade_notation_conversion_is_not_silently_accepted(roman, arabic):
    source = f"장치는 {roman}등급으로 분류하며 외부 전원이 연결되어야 작동한다."
    request, lookup = inputs(source)
    result = rewrite_source(request, lookup, http_client=response(output(
        source.replace(roman, arabic),
    ), []))
    assert result.candidate_texts == ()
    assert result.rejection_reasons == (REJECT_NUMBER_ABSENT,)


def test_partial_candidates_preserve_rejections_without_promoting_a_final_answer():
    request, lookup = inputs()
    result = rewrite_source(request, lookup, http_client=response(output(REWRITE, OTHER), []))
    assert result.candidate_texts == (REWRITE,)
    assert result.rejection_reasons == (REJECT_NUMBER_ABSENT,)
    assert not hasattr(result, "answer_status")


@pytest.mark.parametrize("status", ["not_found", "insufficient_evidence", "conflicting_evidence"])
def test_abstention_and_conflict_are_preserved_as_model_signals(status):
    request, lookup = inputs()
    result = rewrite_source(request, lookup, http_client=response(output(status=status), []))
    assert result.model_status == status and not result.candidate_texts


@pytest.mark.parametrize("bad", [
    None, [], "text", {}, output(), output(TEXT, status="completed"),
    output(TEXT, status="not_found"), output(TEXT, status="insufficient_evidence"),
    output(None), output(123), output(True), output({"text": TEXT}), output(""), output(" "),
    output(TEXT + " " * CLAIM_TEXT_MAX_CHARS), output(*([TEXT] * (MAX_CLAIMS + 1))),
    {**output(TEXT), "sourceChunkIds": ["power-private-id"]},
])
def test_bad_output_is_rejected_without_repair_or_truncation(bad):
    request, lookup = inputs()
    calls = []
    with pytest.raises(SourceBoundRewriteError):
        rewrite_source(request, lookup, http_client=response(bad, calls))
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [
    '{"status":"rewritten","status":"not_found","texts":[]}',
    '{"status":"not_found","texts":[],"texts":[]}',
    '```json\n{"status":"not_found","texts":[]}\n```',
    '{"status":"not_found","texts":[]} extra-private-text',
    '{"status":"private-incomplete-output"',
])
def test_invalid_json_does_not_expose_private_output_in_error(raw):
    request, lookup = inputs()
    with pytest.raises(SourceBoundRewriteError) as caught:
        rewrite_source(request, lookup, http_client=lambda *_: {
            "message": {"content": raw}, "done": True,
        })
    assert str(caught.value) == "invalid_output_json"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("mutation", [
    "empty", "multiple", "history", "question", "blank_question", "unknown", "mismatch",
    "source_text", "truncated", "large", "blank", "hash", "refs",
])
def test_invalid_or_nonisolated_input_never_calls_model(mutation):
    request, lookup = inputs()
    if mutation == "empty":
        request.chunks.clear()
    elif mutation == "multiple":
        request.chunks.append(copy.deepcopy(request.chunks[0]))
    elif mutation == "history":
        request.history.append(QaHistoryTurn("user", "장치의 수치는 999이다."))
    elif mutation == "question":
        request.question = "가" * 2001
    elif mutation == "blank_question":
        request.question = " "
    elif mutation == "unknown":
        del lookup["voltage-private-id"]
    elif mutation == "mismatch":
        lookup["voltage-private-id"].chunk_id = "other-document"
    elif mutation == "source_text":
        request.chunks[0].text = OTHER
    elif mutation == "truncated":
        lookup["voltage-private-id"].text += " 예외 조건은 999이다."
    elif mutation == "large":
        request.chunks[0].text = lookup["voltage-private-id"].text = "가" * 3001
    elif mutation == "blank":
        request.chunks[0].text = lookup["voltage-private-id"].text = " "
    elif mutation == "hash":
        lookup["voltage-private-id"].content_hash = ""
    elif mutation == "refs":
        lookup["voltage-private-id"].source_refs = []
    calls = []
    with pytest.raises(SourceBoundRewriteError):
        rewrite_source(request, lookup, http_client=response(output(TEXT), calls))
    assert not calls


@pytest.mark.parametrize("envelope", [{"done": False}, {"done_reason": "length"},
                                     {"prompt_eval_count": LOCAL_NUM_CTX},
                                     {"error": "private-error"}])
def test_incomplete_transport_is_not_a_candidate(envelope):
    request, lookup = inputs()
    with pytest.raises((SourceBoundRewriteError, SummaryNetworkError)):
        rewrite_source(request, lookup, http_client=response(output(TEXT), [], **envelope))


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"),
                                    STREAM_TOTAL_DEADLINE_SEC + 1])
def test_bad_deadline_never_calls_model(seconds):
    request, lookup = inputs()
    calls = []
    with pytest.raises(SourceBoundRewriteError):
        rewrite_source(request, lookup, deadline_seconds=seconds,
                       http_client=response(output(TEXT), calls))
    assert not calls


def test_network_failure_is_not_retried():
    request, lookup = inputs()
    calls = []
    def fail(*_):
        calls.append(1)
        raise SummaryNetworkError("server_error")
    with pytest.raises(SummaryNetworkError):
        rewrite_source(request, lookup, http_client=fail)
    assert len(calls) == 1


@pytest.mark.parametrize("when", ["before", "during"])
def test_cancellation_never_returns_candidates(when):
    request, lookup = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls = []
    if when == "before":
        signal.cancel()
    def send(*args):
        signal.cancel()
        return response(output(TEXT), calls)(*args)
    with pytest.raises(SummaryCancelled):
        rewrite_source(request, lookup, cancellation_signal=signal, http_client=send)
    assert len(calls) == (0 if when == "before" else 1)


def test_snapshot_change_discards_candidates():
    request, lookup = inputs()
    def send(*args):
        lookup["voltage-private-id"].source_refs[0]["bbox"][0] = -1
        return response(output(TEXT), [])(*args)
    with pytest.raises(SourceBoundRewriteError, match="input_changed"):
        rewrite_source(request, lookup, http_client=send)


def test_same_source_number_relationship_still_requires_semantic_review():
    # 이 사례는 의도적으로 현재 어휘/수치 검사의 상한을 기록한다. 제품에 적용하지 않는다.
    source = "A형 장치의 전압은 24V이고 B형 장치의 전압은 48V이다."
    wrong = "A형 장치의 전압은 48V이고 B형 장치의 전압은 24V이다."
    request, lookup = inputs(source)
    result = rewrite_source(request, lookup, http_client=response(output(wrong), []))
    assert result.candidate_texts == (wrong,)
    assert result.model_status == "rewritten"
    assert not hasattr(result, "verification_status")


def test_expired_deadline_discards_completed_transport(monkeypatch):
    from app.qa_eval.source_bound_rewrite import ProviderRequestBudget

    request, lookup = inputs()
    def send(*args):
        monkeypatch.setattr(ProviderRequestBudget, "remaining_seconds", lambda self: 0)
        return response(output(TEXT), [])(*args)
    with pytest.raises(SummaryNetworkError, match="timeout"):
        rewrite_source(request, lookup, http_client=send)


def test_disallowed_model_is_not_downloaded_or_called():
    request, lookup = inputs()
    calls = []
    with pytest.raises(SourceBoundRewriteError):
        rewrite_source(request, lookup, model="other-model",
                       http_client=response(output(TEXT), calls))
    assert not calls


def test_real_http_path_inherits_cancellation_and_bounded_timeout(monkeypatch):
    from app.services.summary import endpoint

    request, lookup = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls = []
    def post(url, payload, key, **kwargs):
        assert kwargs["is_local"] and 0 < kwargs["timeout"] <= 12
        assert kwargs["cancellation_signal"] is signal and kwargs["max_response_bytes"] > 0
        return response(output(TEXT), calls)(url, payload, key)
    monkeypatch.setattr(endpoint, "post_json", post)
    with summary_cancellation_scope(signal):
        assert rewrite_source(request, lookup, deadline_seconds=12).candidate_texts == (TEXT,)
    assert len(calls) == 1
