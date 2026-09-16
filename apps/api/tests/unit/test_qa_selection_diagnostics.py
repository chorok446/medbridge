"""원문 없는 진단과 선택 정책의 분리, 소켓 원인의 안전한 보존을 검증한다."""

import copy
import json
import urllib.error
import uuid
from dataclasses import asdict

import pytest

from app.qa_eval.evidence_selection import EvidenceSelectionError
from app.qa_eval.selection_diagnostics import (
    SelectionCallStats,
    exception_chain_codes,
)
from app.qa_eval.source_bundle_selection import SELECTION_STRATEGIES, select_source_bundles
from app.services.summary import endpoint
from app.services.summary.cancellation import SummaryCancellationSignal, SummaryCancelled
from app.services.summary.endpoint import SummaryNetworkError
from scripts import evaluate_source_selection as cli
from tests.unit.test_qa_bundle_assessment import assessments
from tests.unit.test_qa_evidence_selection import _client
from tests.unit.test_qa_independent_assessment import OTHER, SAME, STAGE, replies, sequence
from tests.unit.test_qa_source_bundle_selection import inputs, output

PRIVATE = "secret-doc-file-url-token-10054"


def reset_error():
    root = ConnectionResetError(10054, PRIVATE, "private-path")
    root.winerror = 10054
    wrapper = urllib.error.URLError(root)
    exc = SummaryNetworkError("connect_failed", PRIVATE)
    exc.__cause__ = wrapper
    return exc


def test_original_socket_code_is_preserved_without_message_reason_or_path():
    result = exception_chain_codes(reset_error())
    assert result == [
        {"kind": "SummaryNetworkError", "errno": None, "winerror": None},
        {"kind": "URLError", "errno": None, "winerror": None},
        {"kind": "ConnectionResetError", "errno": 10054, "winerror": 10054},
    ]
    assert PRIVATE not in json.dumps(result) and "private-path" not in json.dumps(result)


@pytest.mark.parametrize("value", [True, False, "10054", PRIVATE, 65536, -65536, None])
def test_codes_must_be_bounded_integers_not_text_or_boolean(value):
    exc = OSError(PRIVATE)
    exc.errno, exc.winerror = value, value
    assert exception_chain_codes(exc) == [{"kind": "OSError", "errno": None, "winerror": None}]


def test_unknown_exception_name_and_string_url_reason_are_not_exported_or_parsed():
    sensitive_type = type(PRIVATE, (Exception,), {})
    exc = sensitive_type(PRIVATE)
    exc.__cause__ = urllib.error.URLError(PRIVATE)
    result = exception_chain_codes(exc)
    assert result == [
        {"kind": "other_error", "errno": None, "winerror": None},
        {"kind": "URLError", "errno": None, "winerror": None},
    ]
    assert PRIVATE not in json.dumps(result) and "10054" not in json.dumps(result)


def test_chain_follows_active_cause_or_context_but_is_bounded_and_cycle_safe():
    first, context = ValueError(PRIVATE), TimeoutError(110, PRIVATE)
    first.__context__ = context
    assert exception_chain_codes(first)[1]["kind"] == "TimeoutError"
    first.__suppress_context__ = True
    assert len(exception_chain_codes(first)) == 1
    first.__cause__ = context
    context.__cause__ = first
    assert len(exception_chain_codes(first)) == 2
    root = first
    for _ in range(20):
        exc = OSError(5, PRIVATE)
        exc.__cause__ = root
        root = exc
    assert len(exception_chain_codes(root)) == 8


@pytest.mark.parametrize("strategy", SELECTION_STRATEGIES)
def test_diagnostics_do_not_change_result_payload_options_or_request_count(strategy):
    request, lookup, groups = inputs()
    before = copy.deepcopy((request, lookup, groups))
    results, captures = [], []
    for capture in (False, True):
        stats, calls = SelectionCallStats(capture_steps=capture), []
        responses = (replies() if strategy == "independent_checklist" else
                     [assessments()] if strategy.endswith("checklist") else [output((0, 2))])
        results.append(select_source_bundles(
            request, lookup, groups=groups, strategy=strategy,
            http_client=sequence(responses, calls), stats=stats,
        ))
        captures.append((stats, calls))
        assert stats.requests_started == len(calls)
    assert results[0] == results[1] and captures[0][1] == captures[1][1]
    assert not captures[0][0].steps
    assert len(captures[1][0].steps) == len(captures[1][1])
    assert (request, lookup, groups) == before
    safe = json.dumps(asdict(captures[1][0]), ensure_ascii=False)
    assert "장치" not in safe and "private-heading" not in safe and "chunk_id" not in safe
    assert all(t.phase == "complete" and t.request_started for t in captures[1][0].steps)


def test_trace_maps_singleton_local_zero_to_global_bundle_and_exposes_abstention_step():
    request, lookup, _ = inputs()
    stats = SelectionCallStats(capture_steps=True)
    responses = [assessments((row,)) for row in (SAME, ("unclear", "type"), STAGE)]
    responses.append(assessments())
    result = select_source_bundles(
        request, lookup, groups=[["stage"], ["tail", "head"], ["unrelated"]],
        strategy="independent_checklist", stats=stats, http_client=sequence(responses, []),
    )
    assert result.status == "insufficient_evidence" and not result.outlines
    assert [t.step for t in stats.steps] == [1, 2, 3, 4]
    assert [t.scope for t in stats.steps] == ["bundle", "bundle", "bundle", "global_guard"]
    assert [t.bundle_indices for t in stats.steps] == [(0,), (1,), (2,), (0, 1, 2)]
    assert stats.steps[1].assessments == [{"bundle_index": 1, "target": "unclear", "basis": "type"}]
    assert stats.steps[1].status == "insufficient_evidence"
    assert stats.steps[-1].status == "selected" and stats.steps[-1].conflict is False


@pytest.mark.parametrize("bad", [
    {**assessments((SAME,)), "secret": PRIVATE},
    {"assessments": {"0": {"target": PRIVATE, "basis": "type"}}, "conflict": False},
    {"assessments": {PRIVATE: {"target": "same", "basis": "type"}}, "conflict": False},
])
def test_unvalidated_model_output_is_never_exported(bad):
    request, lookup, groups = inputs()
    stats = SelectionCallStats(capture_steps=True)
    with pytest.raises(EvidenceSelectionError):
        select_source_bundles(request, lookup, groups=groups, strategy="independent_checklist",
                              stats=stats, http_client=_client(bad, []))
    assert stats.requests_started == 1 and len(stats.steps) == 1
    assert stats.steps[0].phase == "assessment" and not stats.steps[0].assessments
    assert stats.steps[0].status is None and PRIVATE not in json.dumps(asdict(stats))


@pytest.mark.parametrize("failure", ["transport", "provenance", "cancel"])
def test_later_failure_keeps_only_completed_prior_assessments_and_reraises(failure):
    request, lookup, groups = inputs()
    stats, calls = SelectionCallStats(capture_steps=True), []
    signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
    original = reset_error()
    def send(*args):
        response = sequence(replies(), calls)(*args)
        if len(calls) == 2 and failure == "transport":
            raise original
        return response
    def guard():
        if len(calls) == 2:
            if failure == "provenance":
                raise original
            if failure == "cancel":
                signal.cancel()
    with pytest.raises((SummaryNetworkError, SummaryCancelled)) as caught:
        select_source_bundles(request, lookup, groups=groups, strategy="independent_checklist",
                              stats=stats, http_client=send, cancellation_signal=signal,
                              after_response=guard)
    assert len(calls) == stats.requests_started == len(stats.steps) == 2
    assert stats.steps[0].phase == "complete" and stats.steps[0].assessments
    assert stats.steps[1].phase == (
        "native_request" if failure == "transport" else "model_provenance")
    assert not stats.steps[1].assessments and stats.steps[1].status is None
    if failure != "cancel":
        assert caught.value is original and stats.steps[1].error_chain[-1]["winerror"] == 10054
    assert PRIVATE not in json.dumps(asdict(stats))
    # 같은 관찰 객체를 다시 써도 이전 사례의 성공/오류가 누출되지 않는다.
    signal.cancel()
    with pytest.raises(SummaryCancelled):
        select_source_bundles(request, lookup, strategy="independent_checklist", stats=stats,
                              cancellation_signal=signal, http_client=send)
    assert stats.requests_started == 0 and stats.steps == []


@pytest.mark.parametrize("diagnostics", [False, True])
async def test_cli_opt_in_error_trace_keeps_failure_unmeasured_and_never_retries(
    monkeypatch, diagnostics,
):
    calls = []
    async def digest(model):
        return "test-digest"
    def post(url, payload, key, **kwargs):
        value = _client(assessments((SAME,)), calls)(url, payload, key)
        if len(calls) == 2:
            raise reset_error()
        return value
    monkeypatch.setattr(cli, "release_model_digest", digest)
    monkeypatch.setattr(cli, "loaded_release_model_digest", digest)
    monkeypatch.setattr(endpoint, "post_json", post)
    args = ["--local", "--suite", "independent", "--strategy", "independent_checklist"]
    if diagnostics:
        args.append("--diagnostics")
    report, code = await cli.run(cli.parse_args(args))
    assert code == 2 and len(calls) == report["model_requests_started"] == 2
    assert report["selection_attempts"] == 1 and not report["gate"]["passed"]
    assert report["results"][0]["status"] == "error"
    assert report["results"][0]["missing_sources"] is None
    assert report["diagnostics_enabled"] is diagnostics
    assert ("steps" in report["results"][0]) is diagnostics
    assert ("error_chain" in report["execution_detail"]) is diagnostics
    if diagnostics:
        assert report["execution_detail"]["error_chain"][-1]["winerror"] == 10054
        assert report["results"][0]["steps"][1]["bundle_indices"] == (1,)
    assert PRIVATE not in json.dumps(report)


def test_conflict_and_negative_scopes_are_visible_without_changing_final_status():
    request, lookup, groups = inputs()
    stats = SelectionCallStats(capture_steps=True)
    outputs = [assessments((OTHER,))] * 3 + [assessments(conflict=True)]
    result = select_source_bundles(request, lookup, groups=groups, strategy="independent_checklist",
                                   stats=stats, http_client=sequence(outputs, []))
    assert result.status == "conflicting_evidence"
    assert all(t.status == "not_found" for t in stats.steps[:3])
    assert stats.steps[-1].conflict is True and stats.steps[-1].scope == "global_guard"
