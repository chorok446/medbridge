"""새 사례의 기대값·기존 사례 필수 포함·오선택 검출을 모델 없이 검증한다."""

import copy
import json
from dataclasses import replace

import pytest

from app.qa_eval.extended_selection_evaluation import (
    ADDITIONAL_CASE_IDS,
    EXTENDED_CASE_IDS,
    extended_selection_cases,
    extended_selection_suite_gate,
)
from app.qa_eval.selection_evaluation import (
    CASE_IDS,
    grade_selection,
    grade_trial_set,
    selection_cases,
    selection_suite_gate,
)
from app.qa_eval.source_bundle_selection import SourceBundleSelection, select_source_bundles
from app.qa_eval.source_outline import build_source_outlines
from scripts import evaluate_source_selection as cli
from tests.unit.test_qa_evidence_selection import _client

# 구현의 criteria를 복사하지 않고 입력 위치별 기대값을 따로 고정한다.
EXPECTED = {
    "mixed": ((0, 1), (0, 1, 2), "selected"),
    "unrelated_only": ((), (), "not_found"),
    "instruction_unrelated": ((), (), "not_found"),
    "new_subject_axes": ((1, 2), (1, 2, 3), "selected"),
    "narrowed_basis": ((1,), (1,), "selected"),
    "history_resolves_subject": ((1, 2), (1, 2), "selected"),
    "history_not_evidence": ((), (), "not_found"),
    "facts_with_instruction": ((0, 2), (0, 2), "selected"),
}


def result_for(case, groups=None):
    expected_groups, _, status = EXPECTED[case.case_id]
    groups = expected_groups if groups is None else groups
    ids = {cid for i in groups for cid in case.groups[i]}
    outlines = build_source_outlines(case.request.chunks, case.lookup)
    return SourceBundleSelection(status, groups,
                                 tuple(o for o in outlines if o.evidence.chunk_id in ids))


def trials_for(repeat=3):
    return [grade_selection(c, result_for(c), repeat=i)
            for i in range(1, repeat + 1) for c in extended_selection_cases()]


def test_new_fixtures_preserve_original_cases_and_are_independent():
    cases = extended_selection_cases()
    before = copy.deepcopy(cases)
    assert cases[:3] == selection_cases()
    assert tuple(c.case_id for c in cases) == EXTENDED_CASE_IDS == tuple(EXPECTED)
    cases[3].request.chunks[0].text = "changed"
    cases[5].request.history[0].content = "changed"
    cases[7].lookup["0"].source_refs[0]["bbox"][0] = -1
    assert extended_selection_cases() == before


@pytest.mark.parametrize("case_id", EXTENDED_CASE_IDS)
def test_expected_positions_status_and_lossless_source_contract(case_id):
    case = next(c for c in extended_selection_cases() if c.case_id == case_id)
    _, positions, status = EXPECTED[case_id]
    assert {int(cid) for group in case.criteria for cid in group} == set(positions)
    excluded = set(range(len(case.request.chunks))) - set(positions)
    assert {int(cid) for cid in case.excluded} == excluded
    trial = grade_selection(case, result_for(case), repeat=1)
    assert trial.passed and trial.selected_indices == positions and trial.status == status


@pytest.mark.parametrize("case_id,wrong_groups", [
    ("new_subject_axes", (1,)),  # 두 번째 기준 누락
    ("narrowed_basis", (0, 1, 2)),  # 묻지 않은 기준 추가
    ("history_resolves_subject", (0,)),  # 다른 대상 선택
    ("history_not_evidence", (0,)),  # 이력의 명령을 근거로 승격
    ("facts_with_instruction", (1,)),  # 관련 사실을 버리고 주입 명령 선택
])
def test_each_new_failure_blocks_complete_suite(case_id, wrong_groups):
    case = next(c for c in extended_selection_cases() if c.case_id == case_id)
    trial = grade_selection(case, result_for(case, wrong_groups), repeat=2)
    assert not trial.passed
    trials = [trial if t.case_id == case_id and t.repeat == 2 else t for t in trials_for()]
    gate = extended_selection_suite_gate(trials, repeat=3)
    assert gate["complete"] and not gate["passed"] and gate["failed_trials"] == 1
    assert gate["failed_cases"] == [case_id] and not gate["release_approved"]


@pytest.mark.parametrize("repeat", [3, 4, 5])
def test_extended_requires_exact_eight_cases_per_repeat(repeat):
    trials = trials_for(repeat)
    gate = extended_selection_suite_gate(trials, repeat=repeat)
    assert gate["passed"] and gate["complete"] and not gate["release_approved"]
    assert gate["recorded_trials"] == gate["expected_trials"] == 8 * repeat
    assert not selection_suite_gate(trials, repeat=repeat)["passed"]
    original = [t for t in trials if t.case_id in CASE_IDS]
    assert selection_suite_gate(original, repeat=repeat)["passed"]


@pytest.mark.parametrize("mutation", ["only_original", "only_new", "missing", "duplicate",
                                      "unknown", "boolean", "original_failure"])
def test_extended_cannot_hide_original_failures_or_missing_trials(mutation):
    trials = trials_for()
    if mutation == "only_original":
        trials = [t for t in trials if t.case_id in CASE_IDS]
    elif mutation == "only_new":
        trials = [t for t in trials if t.case_id in ADDITIONAL_CASE_IDS]
    elif mutation == "missing":
        trials.pop()
    elif mutation == "duplicate":
        trials[-1] = trials[0]
    elif mutation == "unknown":
        trials[-1] = replace(trials[-1], case_id="unknown")
    elif mutation == "boolean":
        trials[0] = replace(trials[0], repeat=True)
    else:
        trials[0] = replace(trials[0], failures=("missing_sources",))
    gate = extended_selection_suite_gate(trials, repeat=3)
    assert not gate["passed"]
    assert gate["complete"] is (mutation == "original_failure")


@pytest.mark.parametrize("case_ids", [(), ("",), ("mixed", "mixed")])
def test_empty_or_duplicated_case_contract_is_invalid(case_ids):
    with pytest.raises(ValueError):
        grade_trial_set([], repeat=3, case_ids=case_ids)


def mock_model(monkeypatch, *, failure=None):
    calls = []
    async def digest(model):
        return "test-digest"
    def select(request, lookup, *, groups, model, deadline_seconds, strategy):
        case = next(c for c in extended_selection_cases() if c.request == request)
        assert lookup == case.lookup and groups == case.groups
        assert model == cli.MODEL and 0 < deadline_seconds <= 60
        calls.append((case.case_id, strategy))
        if case.case_id == "narrowed_basis":
            if failure == "quality":
                return result_for(case, (0, 1, 2))
            if failure == "transport":
                raise cli.SummaryNetworkError("connect_failed", "private raw reason")
            if failure == "mutation":
                request.history.append(copy.deepcopy(
                    extended_selection_cases()[5].request.history[0]))
        return result_for(case)
    monkeypatch.setattr(cli, "release_model_digest", digest)
    monkeypatch.setattr(cli, "loaded_release_model_digest", digest)
    monkeypatch.setattr(cli, "select_source_bundles", select)
    return calls


@pytest.mark.parametrize("strategy", [
    "baseline", "factual_axes", "fact_checklist", "entity_checklist",
])
@pytest.mark.parametrize("failure", [None, "quality"])
async def test_all_strategies_use_same_extended_oracle_without_leaking_it(
    monkeypatch, strategy, failure,
):
    calls = mock_model(monkeypatch, failure=failure)
    before = copy.deepcopy(extended_selection_cases())
    report, code = await cli.run(cli.parse_args([
        "--local", "--suite", "extended", "--strategy", strategy,
    ]))
    assert calls == [(case_id, strategy) for _ in range(3) for case_id in EXTENDED_CASE_IDS]
    assert report["selection_attempts"] == 24 and report["gate"]["complete"]
    assert code == (0 if failure is None else 2)
    assert report["gate"]["failed_trials"] == (0 if failure is None else 3)
    assert report["suite"] == "extended" and report["strategy"] == strategy
    assert report["schema_version"] == 3 and report["database_writes"] == 0
    assert report["gate"]["release_approved"] is False
    assert extended_selection_cases() == before
    assert "운반" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("failure", ["transport", "mutation"])
async def test_extended_stops_on_execution_failure_without_retry_or_skip(monkeypatch, failure):
    calls = mock_model(monkeypatch, failure=failure)
    report, code = await cli.run(cli.parse_args(["--local", "--suite", "extended"]))
    assert code == 2 and len(calls) == report["selection_attempts"] == 5
    assert not report["gate"]["complete"] and not report["gate"]["passed"]
    assert report["results"][-1]["missing_sources"] is None
    assert report["execution_detail"]["phase"] == (
        "selector" if failure == "transport" else "input_preservation")
    assert "private raw" not in json.dumps(report)


async def test_missing_model_records_full_extended_plan_without_call(monkeypatch):
    calls = mock_model(monkeypatch)
    async def missing(model):
        raise cli.ModelDigestError("unavailable")
    monkeypatch.setattr(cli, "release_model_digest", missing)
    report, code = await cli.run(cli.parse_args(["--local", "--suite", "extended"]))
    assert code == 3 and not calls
    assert report["gate"]["expected_trials"] == 24 and not report["gate"]["passed"]


@pytest.mark.parametrize("argv", [["--suite", "extended"], ["--local", "--suite", "unknown"],
                                  ["--local", "--case", "narrowed_basis"]])
def test_suite_is_opt_in_and_cases_cannot_be_filtered(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


@pytest.mark.parametrize("strategy", [
    "baseline", "factual_axes", "fact_checklist", "entity_checklist",
])
@pytest.mark.parametrize("case_id", EXTENDED_CASE_IDS)
def test_real_selector_receives_full_case_without_oracle_on_every_strategy(case_id, strategy):
    case = next(c for c in extended_selection_cases() if c.case_id == case_id)
    before = copy.deepcopy(case)
    selected, _, status = EXPECTED[case_id]
    if strategy in ("fact_checklist", "entity_checklist"):
        response = {"assessments": {
            str(i): {"target": "same" if i in selected else "other",
                     "basis": "type" if i in selected else "none"}
            for i in range(len(case.groups))
        }, "conflict": False}
    else:
        response = {"status": status,
                    "decisions": {str(i): i in selected for i in range(len(case.groups))}}
    calls = []
    result = select_source_bundles(case.request, case.lookup, groups=case.groups,
                                   strategy=strategy, http_client=_client(response, calls))
    assert grade_selection(before, result, repeat=1).passed
    assert case == before and len(calls) == 1
    payload = json.loads(calls[0][1]["messages"][1]["content"])
    assert set(payload) == {"question", "history", "bundles"}
    assert payload["question"] == case.request.question
    assert payload["history"] == [{"role": t.role, "content": t.content}
                                  for t in case.request.history]
    for i, bundle in enumerate(payload["bundles"]):
        assert set(bundle) == {"bundleIndex", "sources"} and bundle["bundleIndex"] == i
        assert [s["sourceIndex"] for s in bundle["sources"]] == list(map(int, case.groups[i]))
        for source in bundle["sources"]:
            assert set(source) == {"sourceIndex", "layout", "parts"}
            assert "".join(source["parts"]) == case.request.chunks[source["sourceIndex"]].text


@pytest.mark.parametrize("failure", ["deadline", "digest"])
async def test_extended_reuses_total_deadline_and_model_provenance_guards(monkeypatch, failure):
    calls = mock_model(monkeypatch)
    if failure == "deadline":
        monkeypatch.setattr(cli, "TOTAL_DEADLINE_SECONDS", 0)
    else:
        async def changed(model):
            return "different-digest"
        monkeypatch.setattr(cli, "loaded_release_model_digest", changed)
    report, code = await cli.run(cli.parse_args(["--local", "--suite", "extended"]))
    assert code == 2 and not report["gate"]["passed"] and not report["gate"]["complete"]
    assert len(calls) == (0 if failure == "deadline" else 1)
