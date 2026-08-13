"""반복 실행 결과 집계 + 변동성(instability). 평균으로 안전 실패를 숨기지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.qa_eval.evaluate import (
    CaseResult,
    is_critical_case_failure,
    is_explicit_safety_violation,
    is_safety_failure,
)


@dataclass
class CaseAggregate:
    case_id: str
    category: str
    safety_critical: bool
    runs: int
    passes: int
    pass_rate: float
    statuses: list[str]
    unstable: bool  # 반복 간 pass 또는 status가 흔들림
    safety_failed: bool  # 반복 중 한 번이라도 게이트 차단 대상(위해 노출 또는 안전중요 실패)
    # §2 분리: 실제 위해 노출 vs 안전 중요 케이스 비위해 실패
    explicit_safety_violation: bool
    critical_case_failure: bool
    # 안전 케이스는 한 번이라도 실패하면 실패로 본다.
    final_pass: bool
    # 대표값(보고용) — 마지막 실행 기준. 문서 원문이 아닌 집계 지표만 담는다.
    claim_count: int
    citation_count: int
    latency_sec: float
    safety_violations: list[str]  # 일반화된 위반 메시지(문서 원문 없음)
    results: list[CaseResult] = field(default_factory=list)  # run별 진단(원문 없음)


@dataclass
class EvalSummary:
    model: str
    total_cases: int
    total_runs: int
    protocol_success_rate: float
    status_accuracy: float
    answerable_valid_rate: float
    not_found_hold_accuracy: float
    safety_failure_cases: int  # 게이트 차단 대상 케이스 수(위해 노출 ∪ 안전중요 실패)
    explicit_safety_violation_cases: int  # 실제 위해 노출 케이스 수
    critical_case_failure_cases: int  # 안전 중요 케이스의 비위해 실패 수
    unstable_cases: int
    category_pass_rate: dict[str, float]
    latency_p50: float
    latency_p95: float
    case_aggregates: list[CaseAggregate] = field(default_factory=list)


def _pct(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _rate_or_vacuous(n: int, d: int) -> float:
    # 적용 대상 케이스가 없으면(분모 0) 게이트에서 거짓 실패가 나지 않도록 1.0(vacuous)로 본다.
    # 출시 모드의 카테고리 커버리지 요구가 실제 누락을 따로 잡는다.
    return round(n / d, 4) if d else 1.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
    return round(ordered[idx], 3)


def aggregate_case(results: list[CaseResult]) -> CaseAggregate:
    """한 케이스의 반복 결과들을 집계한다."""
    assert results, "결과가 비어 있습니다"
    runs = len(results)
    passes = sum(1 for r in results if r.passed)
    statuses = [r.status for r in results]
    safety_failed = any(is_safety_failure(r) for r in results)
    explicit_violation = any(is_explicit_safety_violation(r) for r in results)
    critical_failure = any(is_critical_case_failure(r) for r in results)
    unstable = len({r.passed for r in results}) > 1 or len(set(statuses)) > 1
    critical = results[0].safety_critical
    # 안전 케이스: 한 번이라도 실패면 실패. 그 외: 모든 반복 통과해야 통과로 본다
    # (평균으로 숨기지 않는다 — 통과율은 별도 보고).
    final_pass = (passes == runs) and not safety_failed
    last = results[-1]
    # 반복 전체에서 관측된 위반 메시지(중복 제거, 순서 보존)
    violations: list[str] = []
    for r in results:
        for v in r.safety_violations:
            if v not in violations:
                violations.append(v)
    return CaseAggregate(
        case_id=results[0].case_id,
        category=results[0].category,
        safety_critical=critical,
        runs=runs,
        passes=passes,
        pass_rate=_pct(passes, runs),
        statuses=statuses,
        unstable=unstable,
        safety_failed=safety_failed,
        explicit_safety_violation=explicit_violation,
        critical_case_failure=critical_failure,
        final_pass=final_pass,
        claim_count=last.claim_count,
        citation_count=last.citation_count,
        latency_sec=round(sum(r.latency_sec for r in results) / runs, 3),
        safety_violations=violations,
        results=list(results),
    )


def summarize(model: str, per_case: list[list[CaseResult]]) -> EvalSummary:
    """케이스별 반복 결과 목록 → 전체 요약."""
    aggregates = [aggregate_case(rs) for rs in per_case]
    flat = [r for rs in per_case for r in rs]
    total_runs = len(flat)

    protocol_ok = sum(1 for r in flat if r.checks.get("protocol"))
    status_ok = sum(1 for r in flat if r.checks.get("status"))

    answerable = [
        r
        for r in flat
        if r.category
        in ("grounded_basic", "numeric", "unit", "direction", "polarity", "long_context")
    ]
    answerable_valid = sum(1 for r in answerable if r.status == "completed" and r.passed)

    not_found = [r for r in flat if r.category == "not_found"]
    not_found_hold = sum(1 for r in not_found if r.status in ("not_found", "insufficient_evidence"))

    by_cat: dict[str, list[bool]] = {}
    for agg in aggregates:
        by_cat.setdefault(agg.category, []).append(agg.final_pass)
    category_pass_rate = {cat: _pct(sum(v), len(v)) for cat, v in by_cat.items()}

    latencies = [r.latency_sec for r in flat]
    return EvalSummary(
        model=model,
        total_cases=len(aggregates),
        total_runs=total_runs,
        protocol_success_rate=_pct(protocol_ok, total_runs),
        status_accuracy=_pct(status_ok, total_runs),
        answerable_valid_rate=_rate_or_vacuous(answerable_valid, len(answerable)),
        not_found_hold_accuracy=_rate_or_vacuous(not_found_hold, len(not_found)),
        safety_failure_cases=sum(1 for a in aggregates if a.safety_failed),
        explicit_safety_violation_cases=sum(1 for a in aggregates if a.explicit_safety_violation),
        critical_case_failure_cases=sum(1 for a in aggregates if a.critical_case_failure),
        unstable_cases=sum(1 for a in aggregates if a.unstable),
        category_pass_rate=category_pass_rate,
        latency_p50=_percentile(latencies, 0.5),
        latency_p95=_percentile(latencies, 0.95),
        case_aggregates=aggregates,
    )
