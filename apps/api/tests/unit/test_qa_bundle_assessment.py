"""독립적인 대상/분류 판단 후보의 집계와 기존 출처 보존 가드를 검사한다."""

import copy
import json
import uuid

import pytest

from app.qa_eval.bundle_assessment import (
    ASSESSMENT_SYSTEM,
    ENTITY_ASSESSMENT_SYSTEM,
    assessed_indices,
)
from app.qa_eval.evidence_selection import EvidenceSelectionError
from app.qa_eval.extended_selection_evaluation import extended_selection_cases
from app.qa_eval.selection_evaluation import grade_selection
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


@pytest.mark.parametrize("strategy", ["fact_checklist", "entity_checklist"])
def test_checklist_uses_same_full_input_and_one_call_preserving_whole_bundle(strategy):
    request, lookup, groups = inputs()
    before = copy.deepcopy((request, lookup, groups))
    calls = []
    result = select_source_bundles(request, lookup, groups=groups, strategy=strategy,
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
@pytest.mark.parametrize("strategy", ["fact_checklist", "entity_checklist"])
def test_checklist_keeps_abort_guards_without_retry(monkeypatch, failure, strategy):
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
        select_source_bundles(request, lookup, groups=groups, strategy=strategy,
                              cancellation_signal=signal, http_client=send)
    assert len(calls) == 1


def test_entity_candidate_changes_only_system_not_model_input_schema_or_guards():
    case = extended_selection_cases()[3]
    before = copy.deepcopy(case)
    traces = []
    for strategy in ("fact_checklist", "entity_checklist"):
        calls = []
        select_source_bundles(case.request, case.lookup, groups=case.groups, strategy=strategy,
                              http_client=_client(assessments(), calls))
        traces.append(calls[0])
    old, new = copy.deepcopy(traces)
    assert old[1]["messages"][0]["content"] == ASSESSMENT_SYSTEM
    assert new[1]["messages"][0]["content"] == ENTITY_ASSESSMENT_SYSTEM
    assert ENTITY_ASSESSMENT_SYSTEM.startswith(ASSESSMENT_SYSTEM)
    assert len(ENTITY_ASSESSMENT_SYSTEM) > len(ASSESSMENT_SYSTEM)
    new[1]["messages"][0]["content"] = old[1]["messages"][0]["content"]
    assert new == old and case == before


@pytest.mark.parametrize("strategy", ["fact_checklist", "entity_checklist"])
@pytest.mark.parametrize("state_target", ["other", "same"])
def test_observed_wrong_state_target_is_not_silently_overridden(strategy, state_target):
    case = extended_selection_cases()[3]
    # 2026-09-17 실제 반환 범주: 상태는 stage로 인식하나 대상은 other로 오인했다.
    response = assessments((("other", "none"), ("same", "type"), (state_target, "stage")))
    calls = []
    result = select_source_bundles(case.request, case.lookup, groups=case.groups, strategy=strategy,
                                   http_client=_client(response, calls))
    trial = grade_selection(case, result, repeat=1)
    assert len(calls) == 1
    assert trial.passed is (state_target == "same")
    if state_target == "other":
        assert trial.selected_indices == (1, 2)
        assert trial.failures == ("missing_sources", "incomplete_criteria")
    else:
        assert trial.selected_indices == (1, 2, 3)


@pytest.mark.parametrize("status,rows,conflict", [
    ("not_found", (("other", "stage"), ("same", "none"), ("other", "type")), False),
    ("insufficient_evidence", (("same", "type"), ("same", "unclear"), ("other", "none")), False),
    ("conflicting_evidence", (("same", "type"), ("same", "stage"), ("other", "none")), True),
])
def test_entity_candidate_keeps_abstention_and_conflict_semantics(status, rows, conflict):
    request, lookup, groups = inputs()
    result = select_source_bundles(
        request, lookup, groups=groups, strategy="entity_checklist",
        http_client=_client(assessments(rows, conflict=conflict), []),
    )
    assert result.status == status
    if status != "conflicting_evidence":
        assert not result.outlines and not result.selected_bundle_indices
