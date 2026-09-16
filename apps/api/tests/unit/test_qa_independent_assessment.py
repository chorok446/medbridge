"""독립 호출의 원문 보존·전체 가드·공유 기한·재시도 금지를 모델 없이 검사한다."""

import copy
import json
import uuid

import pytest

from app.qa_eval.bundle_assessment import ASSESSMENT_SYSTEM
from app.qa_eval.evidence_selection import EvidenceSelectionError
from app.qa_eval.source_bundle_selection import (
    ProviderRequestBudget,
    SelectionCallStats,
    select_source_bundles,
)
from app.services.qa.provider import QaContextChunk, QaRequest
from app.services.qa.settings import CONTEXT_MAX_CHUNKS
from app.services.summary import endpoint
from app.services.summary.cancellation import SummaryCancellationSignal, SummaryCancelled
from app.services.summary.endpoint import SummaryNetworkError
from tests.unit.test_qa_bundle_assessment import assessments
from tests.unit.test_qa_evidence_selection import _client
from tests.unit.test_qa_schema import _lookup
from tests.unit.test_qa_source_bundle_selection import inputs
from tests.unit.test_qa_source_outline import assert_lossless

STRATEGY = "independent_checklist"
SAME = ("same", "type")
OTHER = ("other", "type")
STAGE = ("same", "stage")
UNCLEAR = ("unclear", "stage")


def replies():
    return [assessments((row,)) for row in (SAME, OTHER, STAGE)] + [assessments()]


def sequence(responses, calls):
    def send(*args):
        return _client(responses[len(calls)], calls)(*args)
    return send


def select(responses, calls, **kwargs):
    request, lookup, groups = inputs()
    return select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                                 http_client=sequence(responses, calls), **kwargs)


def test_independent_calls_change_only_bundle_scope_and_keep_global_guard():
    request, lookup, groups = inputs()
    before = copy.deepcopy((request, lookup, groups))
    calls, control = [], []
    stats = SelectionCallStats(999)
    result = select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                                   http_client=sequence(replies(), calls), stats=stats)
    select_source_bundles(request, lookup, groups=groups, strategy="fact_checklist",
                          http_client=_client(assessments(), control))
    assert calls[-1] == control[0] and len(calls) == stats.requests_started == 4
    full = json.loads(control[0][1]["messages"][1]["content"])
    for i, (url, payload, key) in enumerate(calls[:3]):
        assert url == control[0][0] and key == ""
        assert payload["messages"][0]["content"] == ASSESSMENT_SYSTEM
        assert payload["options"] == control[0][1]["options"]
        assert payload["think"] is False and payload["stream"] is False
        assert payload["format"]["properties"]["assessments"]["required"] == ["0"]
        data = json.loads(payload["messages"][1]["content"])
        assert data == {**full, "bundles": [{**full["bundles"][i], "bundleIndex": 0}]}
        for source in data["bundles"][0]["sources"]:
            assert "".join(source["parts"]) == request.chunks[source["sourceIndex"]].text
    assert result.status == "selected" and result.selected_bundle_indices == (0, 2)
    assert [o.evidence.chunk_id for o in result.outlines] == ["head", "tail", "stage"]
    for outline in result.outlines:
        ref = lookup[outline.evidence.chunk_id]
        assert_lossless(outline, ref.text)
        assert outline.evidence.source_refs == ref.source_refs
        assert outline.evidence.content_hash == ref.content_hash
    assert (request, lookup, groups) == before
    result.outlines[0].evidence.source_refs[0]["bbox"][0] = -1
    assert (request, lookup, groups) == before
    assert not hasattr(result, "coverage_verified")


def test_canonical_groups_keep_global_source_order_across_calls():
    request, lookup, _ = inputs()
    calls = []
    result = select_source_bundles(
        request, lookup, groups=[["stage"], ["tail", "head"], ["unrelated"]],
        strategy=STRATEGY, http_client=sequence(replies(), calls),
    )
    assert result == select(replies(), []) and len(calls) == 4


@pytest.mark.parametrize("count", [0, 1, CONTEXT_MAX_CHUNKS])
def test_empty_single_and_maximum_input_have_bounded_call_counts(count):
    lookup = _lookup(*[(str(i), "장치의 분류는 가형과 나형이다.") for i in range(count)])
    request = QaRequest("장치의 분류", [
        QaContextChunk(cid, None, ref.text, 1, 1) for cid, ref in lookup.items()
    ])
    expected = count + int(count > 1)
    outputs = [assessments((SAME,)) for _ in range(count)]
    if count > 1:
        outputs.append(assessments((SAME,) * count))
    calls, stats = [], SelectionCallStats(999)
    result = select_source_bundles(request, lookup, strategy=STRATEGY,
                                   http_client=sequence(outputs, calls), stats=stats)
    assert len(calls) == stats.requests_started == expected
    assert result.selected_bundle_indices == tuple(range(count))
    assert result.status == ("selected" if count else "not_found")


@pytest.mark.parametrize("local,global_rows,local_conflict,global_conflict,status,indices", [
    ((SAME, OTHER, STAGE), (SAME, OTHER, OTHER), False, False, "selected", (0, 2)),
    ((SAME, OTHER, STAGE), (SAME, SAME, STAGE), False, False, "selected", (0, 2)),
    ((OTHER,) * 3, (OTHER,) * 3, False, False, "not_found", ()),
    ((OTHER,) * 3, (SAME, OTHER, OTHER), False, False, "insufficient_evidence", ()),
    ((SAME, OTHER, STAGE), (OTHER,) * 3, False, False, "insufficient_evidence", ()),
    ((SAME, UNCLEAR, STAGE), (SAME, OTHER, STAGE), False, False,
     "insufficient_evidence", ()),
    ((SAME, OTHER, STAGE), (SAME, UNCLEAR, STAGE), False, False,
     "insufficient_evidence", ()),
    ((SAME, OTHER, STAGE), (SAME, SAME, STAGE), False, True,
     "conflicting_evidence", (0, 1, 2)),
    ((SAME, OTHER, STAGE), (SAME, OTHER, STAGE), True, False,
     "conflicting_evidence", (0, 2)),
    ((SAME, UNCLEAR, STAGE), (SAME, OTHER, STAGE), False, True,
     "conflicting_evidence", (0, 2)),
])
def test_global_conflict_uncertainty_and_existence_guards(
    local, global_rows, local_conflict, global_conflict, status, indices,
):
    outputs = [assessments((row,), conflict=local_conflict and i == 0)
               for i, row in enumerate(local)]
    outputs.append(assessments(global_rows, conflict=global_conflict))
    result = select(outputs, [])
    assert result.status == status and result.selected_bundle_indices == indices
    if not indices:
        assert result.outlines == ()


@pytest.mark.parametrize("step", [1, 2, 3, 4])
@pytest.mark.parametrize("failure", ["request", "lookup", "groups", "cancel", "deadline",
                                     "transport", "incomplete", "schema"])
def test_any_subcall_failure_discards_everything_and_never_retries(monkeypatch, step, failure):
    request, lookup, groups = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls, stats = [], SelectionCallStats()
    outputs = replies()
    def send(url, payload, key):
        index = len(calls)
        calls.append((url, payload, key))
        if len(calls) == step:
            if failure == "request":
                request.question += " 변경"
            elif failure == "lookup":
                lookup["head"].source_refs[0]["bbox"][0] = -1
            elif failure == "groups":
                groups[0].pop()
            elif failure == "cancel":
                signal.cancel()
            elif failure == "deadline":
                monkeypatch.setattr(ProviderRequestBudget, "remaining_seconds", lambda self: 0)
            elif failure == "transport":
                raise SummaryNetworkError("connect_failed", "test")
            elif failure == "incomplete":
                return {"done": False}
            elif failure == "schema":
                outputs[index] = assessments(())
        return _client(outputs[index], [])(url, payload, key)
    with pytest.raises((EvidenceSelectionError, SummaryCancelled, SummaryNetworkError)):
        select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                              http_client=send, cancellation_signal=signal, stats=stats)
    assert len(calls) == stats.requests_started == step


def test_cancel_before_first_request_and_invalid_partition_reset_stats():
    request, lookup, groups = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    signal.cancel()
    calls, stats = [], SelectionCallStats(999)
    with pytest.raises(SummaryCancelled):
        select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                              cancellation_signal=signal, http_client=sequence([], calls),
                              stats=stats)
    assert not calls and stats.requests_started == 0
    with pytest.raises(EvidenceSelectionError, match="invalid_source_partition"):
        select_source_bundles(request, lookup, groups=[], strategy=STRATEGY,
                              http_client=sequence([], calls), stats=stats)
    assert not calls and stats.requests_started == 0


def test_all_requests_share_elapsed_deadline_and_safe_cancellation(monkeypatch):
    from app.qa_eval import source_bundle_selection as selector
    clock = [100.0]
    budgets, calls, timeouts = [], [], []
    def budget(**kwargs):
        value = ProviderRequestBudget(**kwargs, clock=lambda: clock[0])
        budgets.append(value)
        return value
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    def post(url, payload, key, **kwargs):
        assert kwargs["is_local"] and kwargs["cancellation_signal"] is signal
        assert url == "http://127.0.0.1:11434/api/chat" and key == ""
        timeouts.append(kwargs["timeout"])
        assert budgets[0].request_limit == budgets[0].requests_started
        clock[0] += 1
        return sequence(replies(), calls)(url, payload, key)
    monkeypatch.setattr(selector, "ProviderRequestBudget", budget)
    monkeypatch.setattr(endpoint, "post_json", post)
    request, lookup, groups = inputs()
    result = select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                                   cancellation_signal=signal, deadline_seconds=12)
    assert result.selected_bundle_indices == (0, 2) and len(budgets) == 1
    assert timeouts == [12, 11, 10, 9]


@pytest.mark.parametrize("step", [0, 3])
def test_duplicate_keys_and_wrong_local_index_fail_closed(step):
    outputs = replies()
    calls = []
    def send(*args):
        index = len(calls)
        data = sequence(outputs, calls)(*args)
        if index == step:
            data["message"]["content"] = (
                '{"assessments":{"2":{"target":"same","basis":"type"}},"conflict":false}'
                if step == 0 else '{"assessments":{},"conflict":false,"conflict":false}'
            )
        return data
    request, lookup, groups = inputs()
    with pytest.raises(EvidenceSelectionError):
        select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY, http_client=send)
    assert len(calls) == step + 1


@pytest.mark.parametrize("change", ["cancel", "deadline", "mutation"])
def test_response_guard_runs_inside_shared_safety_checks(monkeypatch, change):
    request, lookup, groups = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls, stats = [], SelectionCallStats()
    def guard():
        if change == "cancel":
            signal.cancel()
        elif change == "deadline":
            monkeypatch.setattr(ProviderRequestBudget, "remaining_seconds", lambda self: 0)
        else:
            lookup["stage"].text += " 변경"
    with pytest.raises((EvidenceSelectionError, SummaryCancelled, SummaryNetworkError)):
        select_source_bundles(request, lookup, groups=groups, strategy=STRATEGY,
                              http_client=sequence(replies(), calls), stats=stats,
                              cancellation_signal=signal, after_response=guard)
    assert len(calls) == stats.requests_started == 1
