"""지난 선택 실패를 정답표로 검출하며 전체 평가의 실패/누락을 성공으로 숨기지 않는다."""

import copy
import json
from dataclasses import asdict, replace

import pytest

from app.qa_eval.selection_evaluation import (
    CASE_IDS,
    grade_selection,
    selection_cases,
    selection_suite_gate,
)
from app.qa_eval.source_bundle_selection import SourceBundleSelection
from app.qa_eval.source_outline import build_source_outlines
from scripts import evaluate_source_selection as cli


def selection(case, groups=None, status=None):
    if groups is None:
        groups = (0, 1) if case.case_id == "mixed" else ()
    ids = {cid for index in groups for cid in case.groups[index]}
    outlines = build_source_outlines(case.request.chunks, case.lookup)
    return SourceBundleSelection(status or case.expected_status, groups,
                                 tuple(o for o in outlines if o.evidence.chunk_id in ids))


def all_trials():
    return [grade_selection(c, selection(c), repeat=i)
            for i in range(1, 4) for c in selection_cases()]


def test_missed_classification_criterion_is_failure_despite_selected_status():
    case = selection_cases()[0]
    trial = grade_selection(case, selection(case, (0,)), repeat=1)
    assert trial.status == "selected" and not trial.passed
    assert trial.selected_indices == (0, 1)
    assert trial.missing_sources == 1 and trial.incomplete_criteria == 1
    assert trial.failures == ("missing_sources", "incomplete_criteria")


def test_instruction_bearing_unrelated_selection_is_failure_despite_preserved_source():
    case = selection_cases()[2]
    trial = grade_selection(case, selection(case, (0,), "selected"), repeat=1)
    assert not trial.passed and trial.unexpected_sources == 1
    assert trial.failures == ("status_mismatch", "unexpected_sources")


def test_equal_source_count_does_not_hide_wrong_source_substitution():
    case = selection_cases()[0]
    trial = grade_selection(case, selection(case, (0, 2)), repeat=1)
    assert len(trial.selected_indices) == 3  # 예상 수도 3개지만 단계 대신 색상을 고름
    assert not trial.passed
    assert trial.missing_sources == trial.unexpected_sources == trial.incomplete_criteria == 1


def test_unknown_status_fails_without_echoing_untrusted_model_text():
    case = selection_cases()[0]
    trial = grade_selection(case, selection(case, status="secret model output"), repeat=1)
    assert not trial.passed and trial.status == "invalid"
    assert "secret model output" not in json.dumps(asdict(trial))


@pytest.mark.parametrize("mutation", ["text", "hash", "refs", "start", "end", "truncated",
                                      "unit_text", "unit_start", "unit_label", "empty_units"])
def test_correct_ids_do_not_hide_changed_conditions_or_provenance(mutation):
    case = selection_cases()[0]
    before = copy.deepcopy(case)
    result = selection(case)
    outline = result.outlines[0]
    evidence = outline.evidence
    if mutation == "text":
        evidence = replace(evidence, text=evidence.text.split("외부")[0])
    elif mutation == "hash":
        evidence = replace(evidence, content_hash="different")
    elif mutation == "refs":
        evidence.source_refs[0]["bbox"][0] = -1
    elif mutation == "start":
        evidence = replace(evidence, start=1)
    elif mutation == "end":
        evidence = replace(evidence, end=evidence.end - 1)
    elif mutation == "truncated":
        evidence = replace(evidence, input_truncated=True)
    else:
        units = list(outline.units)
        if mutation == "unit_text":
            units[0] = replace(units[0], text="다른 조건")
        elif mutation == "unit_start":
            units[0] = replace(units[0], start=1)
        elif mutation == "unit_label":
            units[-1] = replace(units[-1], label_text="X")
        else:
            units = []
        outline = replace(outline, units=tuple(units))
    outline = replace(outline, evidence=evidence)
    changed = replace(result, outlines=(outline, *result.outlines[1:]))
    trial = grade_selection(case, changed, repeat=1)
    assert "source_changed" in trial.failures and not trial.passed
    assert case == before


@pytest.mark.parametrize("mutation", ["partial", "duplicate", "order", "unknown_id",
                                      "unknown_bundle", "duplicate_bundle", "boolean_bundle"])
def test_invalid_selection_cannot_pass_by_preserving_counts(mutation):
    case = selection_cases()[0]
    result = selection(case)
    if mutation == "partial":
        result = replace(result, outlines=result.outlines[1:])
    elif mutation == "duplicate":
        result = replace(result, outlines=(*result.outlines, result.outlines[0]))
    elif mutation == "order":
        result = replace(result, outlines=tuple(reversed(result.outlines)))
    elif mutation == "unknown_id":
        outline = result.outlines[0]
        changed = replace(outline, evidence=replace(outline.evidence, chunk_id="secret-unknown-id"))
        result = replace(result, outlines=(changed, *result.outlines[1:]))
    else:
        indices = {"unknown_bundle": (99,), "duplicate_bundle": (0, 0, 1),
                   "boolean_bundle": (False, 1)}[mutation]
        result = replace(result, selected_bundle_indices=indices)
    trial = grade_selection(case, result, repeat=1)
    assert not trial.passed and "invalid_bundle_selection" in trial.failures
    assert "secret-unknown-id" not in json.dumps(asdict(trial))


@pytest.mark.parametrize("mutation", [
    "missing_label", "overlap", "missing_group", "duplicate_group",
    "empty_criterion", "wrong_status", "stale_text", "stale_hash",
])
def test_invalid_oracle_is_not_used_to_award_pass(mutation):
    case = selection_cases()[0]
    result = selection(case)
    if mutation == "missing_label":
        case = replace(case, excluded=())
    elif mutation == "overlap":
        case = replace(case, excluded=("0", "3"))
    elif mutation == "missing_group":
        case = replace(case, groups=case.groups[:1])
    elif mutation == "duplicate_group":
        case = replace(case, groups=(*case.groups, case.groups[0]))
    elif mutation == "empty_criterion":
        case = replace(case, criteria=(*case.criteria, ()))
    elif mutation == "wrong_status":
        case = replace(case, expected_status="not_found")
    elif mutation == "stale_text":
        case.request.chunks[0].text += " 변조"
    else:
        case.lookup["0"].content_hash = "stale"
    with pytest.raises(ValueError):
        grade_selection(case, result, repeat=1)


def test_fixture_is_fresh_and_expected_sources_never_change_with_previous_run():
    cases = selection_cases()
    before = copy.deepcopy(cases)
    cases[0].request.chunks[0].text = "변경"
    assert selection_cases() == before
    assert tuple(c.case_id for c in before) == CASE_IDS


def test_all_criteria_and_empty_negative_selections_pass_only_this_synthetic_suite():
    gate = selection_suite_gate(all_trials(), repeat=3)
    assert gate["passed"] and gate["complete"] and gate["recorded_trials"] == 9
    assert gate["release_approved"] is False


def test_one_bad_repeat_is_not_hidden_by_other_passes():
    trials = all_trials()
    case = selection_cases()[0]
    trials[3] = grade_selection(case, selection(case, (0,)), repeat=2)
    gate = selection_suite_gate(trials, repeat=3)
    assert not gate["passed"] and gate["complete"]
    assert gate["failed_trials"] == 1 and gate["failed_cases"] == ["mixed"]


@pytest.mark.parametrize("mutation", ["empty", "filtered", "missing_repeat", "duplicate",
                                      "unknown_case", "boolean_repeat"])
def test_missing_or_duplicate_trials_never_pass(mutation):
    trials = all_trials()
    if mutation == "empty":
        trials = []
    elif mutation == "filtered":
        trials = [t for t in trials if t.case_id != "instruction_unrelated"]
    elif mutation == "missing_repeat":
        trials = trials[:-1]
    elif mutation == "duplicate":
        trials[-1] = trials[0]
    elif mutation == "unknown_case":
        trials[-1] = replace(trials[-1], case_id="other")
    else:
        trials[0] = replace(trials[0], repeat=True)
    gate = selection_suite_gate(trials, repeat=3)
    assert not gate["passed"] and not gate["complete"]


@pytest.mark.parametrize("repeat", [0, 1, 2, 6, True, 3.0])
def test_invalid_repeat_is_rejected(repeat):
    with pytest.raises(ValueError):
        selection_suite_gate(all_trials(), repeat=repeat)


@pytest.mark.parametrize("argv", [[], ["--local", "--repeat", "1"],
                                  ["--local", "--timeout", "nan"],
                                  ["--local", "--strategy", "unknown"],
                                  ["--local", "--timeout", "181"]])
def test_cli_requires_opt_in_and_bounded_complete_run(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


def mock_runtime(monkeypatch, *, regression=False, error=False):
    calls = []
    async def digest(model):
        return "fixed-digest"
    def select(request, lookup, *, groups, **kwargs):
        calls.append(copy.deepcopy(request))
        if error:
            raise RuntimeError("secret raw exception and document text")
        case = next(c for c in selection_cases() if c.request == request)
        if regression and case.case_id in ("mixed", "instruction_unrelated"):
            return selection(case, (0,), "selected")
        return selection(case)
    monkeypatch.setattr(cli, "release_model_digest", digest)
    monkeypatch.setattr(cli, "loaded_release_model_digest", digest)
    monkeypatch.setattr(cli, "select_source_bundles", select)
    return calls


@pytest.mark.parametrize("regression", [False, True])
@pytest.mark.parametrize("strategy", [
    "baseline", "factual_axes", "fact_checklist", "entity_checklist",
])
async def test_cli_grades_real_return_values_without_sending_oracle(
    monkeypatch, regression, strategy,
):
    calls = mock_runtime(monkeypatch, regression=regression)
    report, code = await cli.run(cli.parse_args(["--local", "--strategy", strategy]))
    assert len(calls) == report["selection_attempts"] == 9
    assert code == (2 if regression else 0)
    assert report["gate"]["passed"] is not regression
    assert report["gate"]["failed_trials"] == (6 if regression else 0)
    assert report["gate"]["release_approved"] is False
    assert report["strategy"] == strategy and report["schema_version"] == 3
    assert report["suite"] == "fixed"
    assert all(not hasattr(request, "expected_status") for request in calls)
    serialized = json.dumps(report, ensure_ascii=False)
    assert "전압" not in serialized and "source_refs" not in serialized


@pytest.mark.parametrize("strategy", [
    "baseline", "factual_axes", "fact_checklist", "entity_checklist",
])
async def test_cli_routes_strategy_without_exposing_or_changing_expected_contract(
    monkeypatch, strategy,
):
    mock_runtime(monkeypatch)
    original = copy.deepcopy(selection_cases())
    calls = []
    def select(request, lookup, *, groups, model, deadline_seconds, strategy):
        calls.append(strategy)
        case = next(c for c in original if c.request == request)
        assert lookup == case.lookup and groups == case.groups
        assert model == cli.MODEL and 0 < deadline_seconds <= 60
        return selection(case)
    monkeypatch.setattr(cli, "select_source_bundles", select)
    argv = ["--local"] if strategy == "baseline" else ["--local", "--strategy", strategy]
    report, code = await cli.run(cli.parse_args(argv))
    assert calls == [strategy] * 9 and code == 0
    assert selection_cases() == original and report["gate"]["release_approved"] is False


async def test_execution_error_is_not_skip_success_or_retried(monkeypatch):
    calls = mock_runtime(monkeypatch, error=True)
    report, code = await cli.run(cli.parse_args(["--local"]))
    assert len(calls) == 1 and code == 2 and not report["gate"]["passed"]
    assert not report["gate"]["complete"]
    assert report["results"][0]["missing_sources"] is None
    assert report["execution_detail"] == {"phase": "selector", "kind": "unexpected_error",
                                           "category": None}
    assert "secret raw" not in json.dumps(report)


async def test_known_network_error_records_phase_and_category_without_raw_reason(monkeypatch):
    mock_runtime(monkeypatch)
    def fail(*args, **kwargs):
        raise cli.SummaryNetworkError("server_error", "secret response")
    monkeypatch.setattr(cli, "select_source_bundles", fail)
    report, code = await cli.run(cli.parse_args(["--local"]))
    assert code == 2 and report["selection_attempts"] == 1
    assert report["execution_detail"] == {
        "phase": "selector", "kind": "SummaryNetworkError", "category": "server_error",
    }
    assert "secret response" not in json.dumps(report)


async def test_missing_model_is_not_evaluated_not_passed(monkeypatch):
    calls = mock_runtime(monkeypatch)
    async def missing(model):
        raise cli.ModelDigestError("private error")
    monkeypatch.setattr(cli, "release_model_digest", missing)
    report, code = await cli.run(cli.parse_args(["--local"]))
    assert code == 3 and not calls and not report["gate"]["passed"]


async def test_model_change_stops_evaluation_and_blocks_gate(monkeypatch):
    calls = mock_runtime(monkeypatch)
    async def changed(model):
        return "different-digest"
    monkeypatch.setattr(cli, "loaded_release_model_digest", changed)
    report, code = await cli.run(cli.parse_args(["--local"]))
    assert code == 2 and len(calls) == 1 and not report["model_digest_unchanged"]


async def test_total_deadline_prevents_any_further_call(monkeypatch):
    calls = mock_runtime(monkeypatch)
    monkeypatch.setattr(cli, "TOTAL_DEADLINE_SECONDS", 0)
    report, code = await cli.run(cli.parse_args(["--local"]))
    assert code == 2 and not calls and not report["gate"]["complete"]
