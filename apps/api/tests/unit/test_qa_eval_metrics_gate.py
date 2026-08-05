"""집계·변동성·게이트·보고서 테스트."""

import json

import pytest

from app.qa_eval import report
from app.qa_eval.evaluate import CaseResult
from app.qa_eval.gate import (
    ALLOWLIST_EXCLUDED,
    DEFAULT_RECOMMENDED,
    LIGHT_LIMITED,
    RELEASE_HOLD,
    SELECTABLE,
    evaluate_gate,
)
from app.qa_eval.metrics import aggregate_case, summarize


def _result(case_id, category, passed, status, *, safety_critical=False, violations=None,
            checks=None):
    return CaseResult(
        case_id=case_id, category=category, safety_critical=safety_critical, passed=passed,
        status=status, claim_count=1, citation_count=1, latency_sec=0.1, first_claim_sec=0.05,
        checks=checks or {"protocol": True, "status": passed, "ownership": True,
                          "forbidden": True, "numbers": True, "polarity": True,
                          "conflict": True, "evidence": True, "claims_cap": True},
        safety_violations=violations or [],
    )


def test_safety_single_failure_fails_case():
    # 반복 중 한 번이라도 안전 위반이면 케이스 실패(평균으로 숨기지 않는다)
    ok = _result("c", "numeric", True, "completed", safety_critical=True)
    bad = _result("c", "numeric", False, "completed", safety_critical=True,
                  violations=["문서에 없는 수치가 표시됨: 200"])
    agg = aggregate_case([ok, bad, ok])
    assert agg.safety_failed
    assert not agg.final_pass
    assert agg.passes == 2  # 통과율은 별도로 기록


def test_instability_detected():
    a = _result("c", "grounded_basic", True, "completed")
    b = _result("c", "grounded_basic", False, "insufficient_evidence")
    agg = aggregate_case([a, b])
    assert agg.unstable


def test_gate_default_recommended_when_all_pass():
    per_case = [
        [_result(f"g{i}", "grounded_basic", True, "completed")] for i in range(3)
    ] + [
        [_result("nf", "not_found", True, "not_found")],
        [_result("num", "numeric", True, "completed")],
        [_result("unit", "unit", True, "completed")],
        [_result("pol", "polarity", True, "completed")],
        [_result("conf", "conflict", True, "conflicting_evidence")],
    ]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(summary)
    assert gate.safety_passed
    assert gate.verdict == DEFAULT_RECOMMENDED


def test_gate_safety_failure_holds_release():
    per_case = [[_result("num", "numeric", False, "completed", safety_critical=True,
                         violations=["문서에 없는 수치"])]]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(summary)
    assert not gate.safety_passed
    assert gate.verdict == RELEASE_HOLD


def test_gate_4b_safety_failure_excludes_allowlist():
    per_case = [[_result("num", "numeric", False, "completed", safety_critical=True,
                         violations=["문서에 없는 수치"])]]
    summary = summarize("qwen3:4b", per_case)
    gate = evaluate_gate(summary)
    assert gate.verdict == ALLOWLIST_EXCLUDED


def test_gate_critical_case_failure_holds_release_without_explicit():
    # 안전 중요 케이스가 위해 노출 없이 상태 실패(예: 상충 미탐지) → 둘 다 fail-closed 중
    # criticalCasesPassed가 실패해 출시 보류. 위해 노출(explicit)은 통과.
    per_case = [[_result("conf", "conflict", False, "completed", safety_critical=True)]]
    summary = summarize("qwen3:8b", per_case)
    assert summary.explicit_safety_violation_cases == 0
    assert summary.critical_case_failure_cases == 1
    gate = evaluate_gate(summary)
    assert gate.explicit_safety_passed and not gate.critical_cases_passed
    assert not gate.safety_passed
    assert gate.verdict == RELEASE_HOLD


def test_gate_4b_critical_failure_holds_release_not_allowlist_excluded():
    # 4b라도 위해 노출이 아닌 안전 중요 실패는 allowlist 제외가 아니라 출시 보류.
    per_case = [[_result("conf", "conflict", False, "completed", safety_critical=True)]]
    summary = summarize("qwen3:4b", per_case)
    gate = evaluate_gate(summary)
    assert gate.verdict == RELEASE_HOLD


def test_gate_4b_light_limited_when_useful_gate_fails_but_safe():
    # 안전은 통과, 유용성(보류 정확도 등) 미달 → 경량 제한
    per_case = [[_result("g", "grounded_basic", True, "insufficient_evidence")]]
    summary = summarize("qwen3:4b", per_case)
    gate = evaluate_gate(summary)
    assert gate.safety_passed
    assert gate.verdict == LIGHT_LIMITED


def test_empty_denominator_is_vacuous_not_failing():
    # conflict-only 필터 실행 → answerable·not_found 케이스 없음 → 거짓 0.0 실패가 아니라 1.0
    per_case = [[_result("conf", "conflict", True, "conflicting_evidence")]]
    summary = summarize("qwen3:8b", per_case)
    assert summary.answerable_valid_rate == 1.0
    assert summary.not_found_hold_accuracy == 1.0


def test_release_mode_incomplete_coverage_holds():
    # 출시 모드에서 핵심 카테고리가 빠지면(필터링 등) 불완전 → 출시 보류
    per_case = [[_result("g1", "grounded_basic", True, "completed")]]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(
        summary, release_mode=True,
        expected_categories={"grounded_basic", "numeric", "polarity", "conflict"},
        repeat=3,
    )
    assert gate.verdict == RELEASE_HOLD
    assert not gate.model_gate_passed


def test_release_mode_insufficient_repeat_holds():
    per_case = [
        [_result("g", "grounded_basic", True, "completed")],
        [_result("num", "numeric", True, "completed")],
        [_result("pol", "polarity", True, "completed")],
        [_result("conf", "conflict", True, "conflicting_evidence")],
        [_result("nf", "not_found", True, "not_found")],
    ]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(
        summary, release_mode=True,
        expected_categories={c[0].category for c in per_case}, repeat=1,  # 3 미만
    )
    assert gate.verdict == RELEASE_HOLD


# 14b·30b-a3b는 게이트 규칙이 같다(gate.py의 한 분기) — 테스트도 사본 대신 한 벌로 돈다.
@pytest.mark.parametrize("model", ["qwen3:14b", "qwen3:30b-a3b"])
def test_high_tier_not_selectable_when_model_gate_fails(model):
    # 안전 통과지만 모델 게이트 실패(보류 정확도 낮음) → selectable 아님
    per_case = [[_result("g", "grounded_basic", True, "insufficient_evidence")]]
    summary = summarize(model, per_case)
    gate = evaluate_gate(summary)
    assert gate.safety_passed
    assert not gate.model_gate_passed
    assert gate.verdict == RELEASE_HOLD


@pytest.mark.parametrize("model", ["qwen3:14b", "qwen3:30b-a3b"])
def test_high_tier_selectable_when_gates_pass(model):
    # 안전·모델 게이트 모두 통과 → '선택 가능'(기본 추천은 여전히 8B).
    per_case = [[_result("g", "grounded_basic", True, "completed")]]
    summary = summarize(model, per_case)
    gate = evaluate_gate(summary)
    assert gate.model_gate_passed
    assert gate.verdict == SELECTABLE


def test_report_json_has_no_leak_and_valid_shape(tmp_path):
    per_case = [[_result("g1", "grounded_basic", True, "completed")]]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(summary)
    jp, mp = report.write_reports(tmp_path, summary, gate, generated_at="2026-08-01T00:00:00Z")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["model"] == "qwen3:8b"
    assert "gate" in data and "cases" in data
    # 케이스 항목은 집계 지표·안전 분류값만 담고 질문·문서·주장 텍스트는 없어야 한다
    case_keys = set(data["cases"][0])
    assert case_keys == {"caseId", "category", "safetyCritical", "runs", "passes",
                         "passRate", "statuses", "answerStatus", "claimCount",
                         "citationCount", "latencySec", "unstable", "safetyFailed",
                         "explicitSafetyViolation", "criticalCaseFailure",
                         "safetyViolations", "finalPass", "runDiagnostics"}
    # §2 분리 지표가 요약·게이트에 담긴다
    assert "explicitSafetyViolationCases" in data["summary"]
    assert "criticalCaseFailureCases" in data["summary"]
    assert "explicitSafetyPassed" in data["gate"]
    assert "criticalCasesPassed" in data["gate"]
    # 스펙 허용 필드가 실제로 담긴다
    assert data["cases"][0]["claimCount"] == 1
    assert data["cases"][0]["answerStatus"] == "completed"
    # run별 진단은 안전 분류값만 담는다(원문 키 없음)
    diag = data["cases"][0]["runDiagnostics"][0]
    assert diag["runIndex"] == 0 and diag["terminalStatus"] == "completed"
    assert set(diag) == {
        "runIndex", "terminalStatus", "passed", "failedChecks", "failureCategory",
        "providerErrorCategory", "prestreamErrorCategory", "timedOut", "started",
        "reachedTerminal", "emittedClaimCount", "rejectedClaimCount",
        "rejectionReasonCodes", "citationCount", "firstClaimSec", "latencySec",
        "explicitSafetyViolation", "criticalCaseFailure",
    }
    assert "질문" not in mp.read_text(encoding="utf-8")
