"""집계·변동성·게이트·보고서 테스트."""

import json

from app.qa_eval import report
from app.qa_eval.evaluate import CaseResult
from app.qa_eval.gate import (
    ALLOWLIST_EXCLUDED,
    DEFAULT_RECOMMENDED,
    LIGHT_LIMITED,
    RELEASE_HOLD,
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


def test_gate_4b_light_limited_when_useful_gate_fails_but_safe():
    # 안전은 통과, 유용성(보류 정확도 등) 미달 → 경량 제한
    per_case = [[_result("g", "grounded_basic", True, "insufficient_evidence")]]
    summary = summarize("qwen3:4b", per_case)
    gate = evaluate_gate(summary)
    assert gate.safety_passed
    assert gate.verdict == LIGHT_LIMITED


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


def test_14b_not_selectable_when_model_gate_fails():
    # 안전 통과지만 모델 게이트 실패(보류 정확도 낮음) → 14b는 selectable 아님
    per_case = [[_result("g", "grounded_basic", True, "insufficient_evidence")]]
    summary = summarize("qwen3:14b", per_case)
    gate = evaluate_gate(summary)
    assert gate.safety_passed
    assert not gate.model_gate_passed
    assert gate.verdict == RELEASE_HOLD


def test_report_json_has_no_leak_and_valid_shape(tmp_path):
    per_case = [[_result("g1", "grounded_basic", True, "completed")]]
    summary = summarize("qwen3:8b", per_case)
    gate = evaluate_gate(summary)
    jp, mp = report.write_reports(tmp_path, summary, gate, generated_at="2026-08-01T00:00:00Z")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["model"] == "qwen3:8b"
    assert "gate" in data and "cases" in data
    # 케이스 항목에 질문·문서·주장 텍스트가 없어야 한다
    case_keys = set(data["cases"][0])
    assert case_keys == {"caseId", "category", "safetyCritical", "runs", "passes",
                         "passRate", "statuses", "unstable", "safetyFailed", "finalPass"}
    assert "질문" not in mp.read_text(encoding="utf-8")
