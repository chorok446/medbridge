"""새 자료의 별도 정답표와 실제 CLI/선택기 다중 요청 집계를 고정한다."""

import copy
import json
from dataclasses import replace

import pytest

from app.qa_eval.independent_selection_evaluation import (
    INDEPENDENT_CASE_IDS,
    independent_selection_cases,
    independent_selection_suite_gate,
)
from app.qa_eval.selection_evaluation import grade_selection
from app.qa_eval.source_bundle_selection import SourceBundleSelection
from app.qa_eval.source_outline import build_source_outlines
from app.services.summary import endpoint
from app.services.summary.endpoint import SummaryNetworkError
from scripts import evaluate_source_selection as cli
from tests.unit.test_qa_evidence_selection import _client
from tests.unit.test_qa_extended_selection_evaluation import EXPECTED as EXTENDED_EXPECTED

# fixture의 criteria를 복사하지 않는 별도의 위치/상태 계약이다.
EXPECTED = {
    "sensor_axes": ((0, 2), (0, 2, 3), "selected"),
    "lamp_state_only": ((1,), (1,), "selected"),
    "multi_bundle_injection_negative": ((), (), "not_found"),
}


def correct_trials():
    trials = []
    for repeat in range(1, 4):
        for case in independent_selection_cases():
            groups, positions, status = EXPECTED[case.case_id]
            all_outlines = build_source_outlines(case.request.chunks, case.lookup)
            result = SourceBundleSelection(status, groups,
                                           tuple(all_outlines[i] for i in positions))
            trials.append(grade_selection(case, result, repeat=repeat))
    return trials


def test_new_fixtures_have_independent_oracles_and_no_original_case_overlap():
    cases = independent_selection_cases()
    before = copy.deepcopy(cases)
    assert tuple(c.case_id for c in cases) == tuple(EXPECTED) == INDEPENDENT_CASE_IDS
    assert not set(INDEPENDENT_CASE_IDS) & set(EXTENDED_EXPECTED)
    assert all(t.passed for t in correct_trials())
    for case in cases:
        _, positions, _ = EXPECTED[case.case_id]
        assert {int(cid) for group in case.criteria for cid in group} == set(positions)
        assert set(map(int, case.excluded)) == set(range(len(case.lookup))) - set(positions)
    cases[0].request.chunks[0].text = "changed"
    cases[1].lookup["1"].source_refs[0]["bbox"][0] = -1
    assert independent_selection_cases() == before


@pytest.mark.parametrize("change", ["none", "missing", "duplicate", "wrong", "foreign"])
def test_new_gate_requires_all_nine_trials_and_never_approves_release(change):
    trials = correct_trials()
    if change == "missing":
        trials.pop()
    elif change == "duplicate":
        trials[-1] = trials[0]
    elif change == "wrong":
        trials[-1] = replace(trials[-1], failures=("unexpected_sources",))
    elif change == "foreign":
        trials[-1] = replace(trials[-1], case_id="mixed")
    gate = independent_selection_suite_gate(trials, repeat=3)
    assert gate["passed"] is (change == "none")
    assert gate["complete"] is (change in ("none", "wrong"))
    assert gate["expected_trials"] == 9 and not gate["release_approved"]


@pytest.mark.parametrize("suite,cases,oracle,attempts,native_calls", [
    ("extended", cli.extended_selection_cases, EXTENDED_EXPECTED, 24, 69),
    ("independent", independent_selection_cases, EXPECTED, 9, 36),
])
@pytest.mark.parametrize("strategy", ["fact_checklist", "independent_checklist"])
@pytest.mark.parametrize("failure", [None, "transport"])
async def test_real_cli_counts_native_calls_without_exposing_oracle_or_hiding_failure(
    monkeypatch, suite, cases, oracle, attempts, native_calls, strategy, failure,
):
    calls = []
    remaining = list(cases()) * 3
    call_in_case = 0
    async def digest(model):
        return "test-digest"
    def post(url, payload, key, **kwargs):
        nonlocal call_in_case
        case = remaining[0]
        data = json.loads(payload["messages"][1]["content"])
        assert set(data) == {"question", "history", "bundles"}
        assert data["question"] == case.request.question
        assert data["history"] == [{"role": t.role, "content": t.content}
                                   for t in case.request.history]
        positions = oracle[case.case_id][1]
        rows = {}
        for bundle in data["bundles"]:
            indices = [s["sourceIndex"] for s in bundle["sources"]]
            assert tuple(map(str, indices)) in case.groups
            for source in bundle["sources"]:
                assert set(source) == {"sourceIndex", "layout", "parts"}
                assert "".join(source["parts"]) == case.request.chunks[source["sourceIndex"]].text
            include = any(i in positions for i in indices)
            rows[str(bundle["bundleIndex"])] = {
                "target": "same" if include else "other", "basis": "type",
            }
        response = _client({"assessments": rows, "conflict": False}, calls)(url, payload, key)
        if failure and len(calls) == 2:
            raise SummaryNetworkError("connect_failed", "sensitive reason")
        call_in_case += 1
        expected = (len(case.groups) + int(len(case.groups) > 1)
                    if strategy == "independent_checklist" else 1)
        if call_in_case == expected:
            remaining.pop(0)
            call_in_case = 0
        return response
    monkeypatch.setattr(cli, "release_model_digest", digest)
    monkeypatch.setattr(cli, "loaded_release_model_digest", digest)
    monkeypatch.setattr(endpoint, "post_json", post)
    report, code = await cli.run(cli.parse_args([
        "--local", "--suite", suite, "--strategy", strategy,
    ]))
    expected_calls = (2 if failure else native_calls
                      if strategy == "independent_checklist" else attempts)
    assert len(calls) == report["model_requests_started"] == expected_calls
    assert sum(r["model_requests_started"] for r in report["results"]) == expected_calls
    assert code == (2 if failure else 0)
    assert report["gate"]["passed"] is (failure is None)
    assert report["gate"]["release_approved"] is False
    assert "sensitive reason" not in json.dumps(report)
    if failure:
        expected_attempts = 1 if strategy == "independent_checklist" else 2
        assert report["selection_attempts"] == expected_attempts
        assert report["results"][-1]["missing_sources"] is None
        assert report["execution_detail"]["category"] == "connect_failed"
    else:
        assert report["selection_attempts"] == attempts
        assert report["model_digest_unchanged"]


async def test_intermediate_digest_change_aborts_even_if_final_digest_recovers(monkeypatch):
    calls, observations = [], []
    async def installed(model):
        return "original"
    async def loaded(model):
        observations.append(len(calls))
        return "changed" if len(calls) == 2 and observations.count(2) == 1 else "original"
    def post(url, payload, key, **kwargs):
        return _client({"assessments": {"0": {"target": "same", "basis": "type"}},
                        "conflict": False}, calls)(url, payload, key)
    monkeypatch.setattr(cli, "release_model_digest", installed)
    monkeypatch.setattr(cli, "loaded_release_model_digest", loaded)
    monkeypatch.setattr(endpoint, "post_json", post)
    report, code = await cli.run(cli.parse_args([
        "--local", "--suite", "independent", "--strategy", "independent_checklist",
    ]))
    assert code == 2 and len(calls) == report["model_requests_started"] == 2
    assert observations == [1, 2, 2] and report["model_digest_unchanged"]
    assert report["execution_detail"]["phase"] == "model_provenance"
    assert not report["gate"]["passed"] and not report["gate"]["complete"]
