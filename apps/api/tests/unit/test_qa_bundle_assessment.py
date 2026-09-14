"""독립적인 대상/분류 판단 후보의 집계와 기존 출처 보존 가드를 검사한다."""

import copy
import json
import uuid

import pytest

from app.qa_eval.bundle_assessment import assessed_indices
from app.qa_eval.evidence_selection import EvidenceSelectionError
from app.qa_eval.source_bundle_selection import select_source_bundles
from app.services.summary.cancellation import SummaryCancellationSignal, SummaryCancelled
from app.services.summary.endpoint import SummaryNetworkError
from tests.unit.test_qa_evidence_selection import _client
from tests.unit.test_qa_source_bundle_selection import inputs, output
from tests.unit.test_qa_source_outline import assert_lossless


def assessments(rows=(("same", "type"), ("other", "type"), ("same", "stage")), *, conflict=False):
    return {"assessments": {str(i): {"target": target, "basis": basis}
                            for i, (target, basis) in enumerate(rows)}, "conflict": conflict}


def invalid_row(row):
    result = assessments()
    result["assessments"]["0"] = row
    return json.dumps(result)


@pytest.mark.parametrize(("rows", "conflict", "status", "indices"), [
    ((("same", "type"), ("same", "stage"), ("same", "grade")), False, "selected", {0, 1, 2}),
    ((("same", "other_classification"),), False, "selected", {0}),
    ((("other", "type"), ("same", "none")), False, "not_found", set()),
    ((("same", "type"), ("same", "unclear")), False, "insufficient_evidence", set()),
    ((("unclear", "stage"),), False, "insufficient_evidence", set()),
    ((("unclear", "unclear"),), False, "insufficient_evidence", set()),
    ((("other", "unclear"),), False, "not_found", set()),
    ((("same", "type"),), True, "conflicting_evidence", {0}),
    ((("same", "unclear"),), True, "conflicting_evidence", set()),
])
def test_only_same_subject_with_actual_classification_is_selected(rows, conflict, status, indices):
    result = assessed_indices(json.dumps(assessments(rows, conflict=conflict)), len(rows))
    assert result == (status, indices)


@pytest.mark.parametrize("bad", [
    "null", "[]", "not json", json.dumps(output()),
    '{"assessments":{},"conflict":false,"conflict":false}',
    '{"assessments":{"0":{"target":"same","target":"other","basis":"type"}},'
    '"conflict":false}',
    invalid_row({"target": "same", "basis": "unknown"}),
    invalid_row({"target": True, "basis": "type"}),
    invalid_row({"target": "same", "basis": []}),
    json.dumps(assessments(conflict=1)),
    json.dumps({**assessments(), "explanation": "untrusted prose"}),
    invalid_row({"target": "same"}),
    invalid_row([]),
    json.dumps(assessments(())),
])
def test_invalid_incomplete_or_duplicate_assessments_fail_closed(bad):
    with pytest.raises(EvidenceSelectionError):
        assessed_indices(bad, 3)


def test_checklist_uses_same_full_input_and_one_call_preserving_whole_bundle():
    request, lookup, groups = inputs()
    before = copy.deepcopy((request, lookup, groups))
    calls = []
    result = select_source_bundles(request, lookup, groups=groups, strategy="fact_checklist",
                                   http_client=_client(assessments(), calls))
    baseline_calls = []
    select_source_bundles(request, lookup, groups=groups,
                          http_client=_client(output(), baseline_calls))
    assert len(calls) == 1 and calls[0][0] == baseline_calls[0][0]
    payload, baseline = calls[0][1], baseline_calls[0][1]
    assert payload["messages"][1] == baseline["messages"][1]
    assert payload["options"] == baseline["options"]
    assert payload["think"] is False and payload["stream"] is False
    schema = payload["format"]
    assert set(schema["required"]) == {"assessments", "conflict"}
    assert schema["properties"]["assessments"]["required"] == ["0", "1", "2"]
    assert result.status == "selected" and result.selected_bundle_indices == (0, 2)
    assert [o.evidence.chunk_id for o in result.outlines] == ["head", "tail", "stage"]
    assert not hasattr(result, "coverage_verified") and (request, lookup, groups) == before
    for outline in result.outlines:
        ref = lookup[outline.evidence.chunk_id]
        assert_lossless(outline, ref.text)
        assert outline.evidence.source_refs == ref.source_refs
        assert outline.evidence.content_hash == ref.content_hash


@pytest.mark.parametrize("failure", ["mutation", "cancel", "deadline", "transport"])
def test_checklist_keeps_abort_guards_without_retry(monkeypatch, failure):
    from app.qa_eval.source_bundle_selection import ProviderRequestBudget
    request, lookup, groups = inputs()
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    calls = []
    def send(*args):
        if failure == "mutation":
            request.chunks[0].text += " 변경"
        elif failure == "cancel":
            signal.cancel()
        elif failure == "deadline":
            monkeypatch.setattr(ProviderRequestBudget, "remaining_seconds", lambda self: 0)
        return _client(assessments(), calls, done=failure != "transport")(*args)
    with pytest.raises((EvidenceSelectionError, SummaryCancelled, SummaryNetworkError)):
        select_source_bundles(request, lookup, groups=groups, strategy="fact_checklist",
                              cancellation_signal=signal, http_client=send)
    assert len(calls) == 1
