"""평가 오케스트레이션 — 데이터셋 × 반복을 실제 경로로 실행하고 집계·게이트·보고서를 낸다."""

from __future__ import annotations

from app.qa_eval.evaluate import CaseResult, evaluate_case
from app.qa_eval.gate import GateResult, evaluate_gate
from app.qa_eval.manifest import Dataset
from app.qa_eval.metrics import EvalSummary, summarize
from app.qa_eval.run_case import run_case


async def run_evaluation(
    factory,
    dataset: Dataset,
    *,
    model_label: str,
    provider_mode: str,  # "deterministic" | "local"
    model: str | None = None,
    repeat: int = 1,
    categories: list[str] | None = None,
    timeout_sec: float = 120.0,
) -> tuple[EvalSummary, GateResult, list[list[CaseResult]]]:
    """반환: (요약, 게이트, 케이스별 반복 결과)."""
    cases = [
        c for c in dataset.cases
        if categories is None or c.category in categories
    ]
    # 필터 없는 전체 실행만 출시 평가로 본다(필터링된 진단 실행은 게이트를 통과시키지 않는다).
    release_mode = categories is None
    expected_categories = {c.category for c in dataset.cases}
    per_case: list[list[CaseResult]] = []
    for case in cases:
        fixture = dataset.fixtures[case.document_fixture]
        results: list[CaseResult] = []
        for _ in range(max(1, repeat)):
            run = await run_case(
                factory, case, fixture,
                provider_mode=provider_mode, model=model, timeout_sec=timeout_sec,
            )
            results.append(evaluate_case(case, run))
        per_case.append(results)

    summary = summarize(model_label, per_case)
    gate = evaluate_gate(
        summary, release_mode=release_mode,
        expected_categories=expected_categories, repeat=repeat,
    )
    return summary, gate, per_case
