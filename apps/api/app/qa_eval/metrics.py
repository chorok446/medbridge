"""반복 실행 결과 집계 + 변동성(instability). 평균으로 안전 실패를 숨기지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.qa_eval.evaluate import CaseResult, is_safety_failure


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
    safety_failed: bool  # 반복 중 한 번이라도 안전 위반
    # 안전 케이스는 한 번이라도 실패하면 실패로 본다.
    final_pass: bool


@dataclass
class EvalSummary:
    model: str
    total_cases: int
    total_runs: int
    protocol_success_rate: float
    status_accuracy: float
    answerable_valid_rate: float
    not_found_hold_accuracy: float
    safety_failure_cases: int
    unstable_cases: int
    category_pass_rate: dict[str, float]
    latency_p50: float
    latency_p95: float
    case_aggregates: list[CaseAggregate] = field(default_factory=list)


def _pct(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


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
    unstable = len({r.passed for r in results}) > 1 or len(set(statuses)) > 1
    critical = results[0].safety_critical
    # 안전 케이스: 한 번이라도 실패면 실패. 그 외: 모든 반복 통과해야 통과로 본다
    # (평균으로 숨기지 않는다 — 통과율은 별도 보고).
    final_pass = (passes == runs) and not safety_failed
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
        final_pass=final_pass,
    )


def summarize(model: str, per_case: list[list[CaseResult]]) -> EvalSummary:
    """케이스별 반복 결과 목록 → 전체 요약."""
    aggregates = [aggregate_case(rs) for rs in per_case]
    flat = [r for rs in per_case for r in rs]
    total_runs = len(flat)

    protocol_ok = sum(1 for r in flat if r.checks.get("protocol"))
    status_ok = sum(1 for r in flat if r.checks.get("status"))

    answerable = [r for r in flat if r.category in (
        "grounded_basic", "numeric", "unit", "direction", "polarity", "long_context"
    )]
    answerable_valid = sum(1 for r in answerable if r.status == "completed" and r.passed)

    not_found = [r for r in flat if r.category == "not_found"]
    not_found_hold = sum(1 for r in not_found if r.status in ("not_found", "insufficient_evidence"))

    by_cat: dict[str, list[bool]] = {}
    for agg in aggregates:
        by_cat.setdefault(agg.category, []).append(agg.final_pass)
    category_pass_rate = {
        cat: _pct(sum(v), len(v)) for cat, v in by_cat.items()
    }

    latencies = [r.latency_sec for r in flat]
    return EvalSummary(
        model=model,
        total_cases=len(aggregates),
        total_runs=total_runs,
        protocol_success_rate=_pct(protocol_ok, total_runs),
        status_accuracy=_pct(status_ok, total_runs),
        answerable_valid_rate=_pct(answerable_valid, len(answerable)),
        not_found_hold_accuracy=_pct(not_found_hold, len(not_found)),
        safety_failure_cases=sum(1 for a in aggregates if a.safety_failed),
        unstable_cases=sum(1 for a in aggregates if a.unstable),
        category_pass_rate=category_pass_rate,
        latency_p50=_percentile(latencies, 0.5),
        latency_p95=_percentile(latencies, 0.95),
        case_aggregates=aggregates,
    )
