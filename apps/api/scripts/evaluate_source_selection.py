"""고정 합성 출처 선택 게이트. --local 명시 시만 모델 호출; 사용자 DB·파일 쓰기 없음.

0=합성 스위트 통과(출시 승인 아님), 2=품질/완결성/실행 실패, 3=모델 미평가.
정답표를 모델에 보내지 않으며 실제 선택기 결과를 그대로 채점한다.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qa_eval.evidence_selection import EvidenceSelectionError  # noqa: E402
from app.qa_eval.extended_selection_evaluation import (  # noqa: E402
    extended_selection_cases,
    extended_selection_suite_gate,
)
from app.qa_eval.independent_selection_evaluation import (  # noqa: E402
    independent_selection_cases,
    independent_selection_suite_gate,
)
from app.qa_eval.ollama import (  # noqa: E402
    ModelDigestError,
    loaded_release_model_digest,
    release_model_digest,
)
from app.qa_eval.selection_diagnostics import exception_chain_codes  # noqa: E402
from app.qa_eval.selection_evaluation import (  # noqa: E402
    MAX_REPEAT,
    MIN_REPEAT,
    SelectionTrial,
    grade_selection,
    selection_cases,
    selection_suite_gate,
)
from app.qa_eval.source_bundle_selection import (  # noqa: E402
    SELECTION_STRATEGIES,
    SelectionCallStats,
    select_source_bundles,
)
from app.qa_eval.source_outline import SourceOutlineError  # noqa: E402
from app.services.summary.endpoint import SummaryNetworkError  # noqa: E402

MODEL = "qwen3:8b"
TOTAL_DEADLINE_SECONDS = 600


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="합성 출처 선택 품질 게이트")
    parser.add_argument("--local", action="store_true", required=True, help="로컬 모델 호출 승인")
    parser.add_argument("--diagnostics", action="store_true",
                        help="원문 없는 하위 판단 범주와 예외 종류/번호 수집")
    parser.add_argument("--repeat", type=int, default=MIN_REPEAT, help="전체 사례 반복 3~5회")
    parser.add_argument("--timeout", type=float, default=60,
                        help="한 사례 전체 제한 0초 초과~180초")
    parser.add_argument("--strategy", choices=SELECTION_STRATEGIES, default="baseline",
                        help="선택 지침 비교; 기본값은 기존 baseline")
    parser.add_argument("--suite", choices=("fixed", "extended", "independent"), default="fixed",
                        help="fixed=기존 3, extended=기존 전체 8, independent=별도 신규 3사례")
    args = parser.parse_args(argv)
    if not MIN_REPEAT <= args.repeat <= MAX_REPEAT:
        parser.error("반복은 3~5회여야 합니다.")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 180:
        parser.error("호출 제한은 0초 초과~180초여야 합니다.")
    return args


async def run(args: argparse.Namespace) -> tuple[dict, int]:
    started = time.monotonic()
    cases, gate = {
        "fixed": (selection_cases, selection_suite_gate),
        "extended": (extended_selection_cases, extended_selection_suite_gate),
        "independent": (independent_selection_cases, independent_selection_suite_gate),
    }[args.suite]
    report: dict = {"schema_version": 5, "model": MODEL, "provider": "local",
                    "diagnostics_enabled": args.diagnostics,
                    "strategy": args.strategy, "suite": args.suite,
                    "repeat": args.repeat, "database_writes": 0, "selection_attempts": 0,
                    "model_requests_started": 0,
                    "model_digest_unchanged": False, "results": []}
    try:
        digest = await release_model_digest(MODEL)
    except ModelDigestError:
        report.update({"gate": gate([], repeat=args.repeat),
                       "execution_failure": "model_not_evaluated"})
        return report, 3
    report["model_digest"] = digest
    trials = []
    for repeat in range(1, args.repeat + 1):
        for case in cases():
            remaining = TOTAL_DEADLINE_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                report["execution_failure"] = "suite_deadline"
                break
            oracle = copy.deepcopy(case)
            call_started = time.monotonic()
            report["selection_attempts"] += 1
            phase = "selector"
            stats = SelectionCallStats(capture_steps=args.diagnostics)
            def verify_response_model() -> None:
                nonlocal phase
                # 선택기 전용 worker에서 각 실제 응답 직후 검사한다. 마지막 묶음만
                # 확인하면 중간 요청에서 모델이 바뀌었다 돌아온 경우를 놓칠 수 있다.
                phase = "model_provenance"
                if asyncio.run(loaded_release_model_digest(MODEL)) != digest:
                    raise ModelDigestError("model_changed")
                phase = "selector"
            try:
                selection = await asyncio.to_thread(
                    select_source_bundles, case.request, case.lookup, groups=case.groups,
                    strategy=args.strategy,
                    model=MODEL, deadline_seconds=min(args.timeout, remaining),
                    stats=stats,
                    after_response=verify_response_model,
                )
                phase = "grading"
                trial = grade_selection(oracle, selection, repeat=repeat)
                phase = "input_preservation"
                if case != oracle:
                    raise ValueError("input_changed")
                phase = "model_provenance"
                if await loaded_release_model_digest(MODEL) != digest:
                    raise ValueError("model_changed")
            except Exception as exc:
                # 예외 원문·응답·경로는 내보내지 않는다. 중단을 실패로 기록하고 재시도하지 않는다.
                trial = SelectionTrial(case.case_id, repeat, "error", (), None, None, None,
                                       ("evaluation_error",))
                report["execution_failure"] = "evaluation_error"
                known = (SummaryNetworkError, EvidenceSelectionError, SourceOutlineError,
                         ModelDigestError, ValueError)
                report["execution_detail"] = {
                    "phase": phase,
                    "kind": type(exc).__name__ if type(exc) in known else "unexpected_error",
                    "category": (exc.category if isinstance(exc, SummaryNetworkError)
                                 and exc.category in {"timeout", "connect_failed", "server_error",
                                                      "context_limit", "rate_limited"} else None),
                }
                if args.diagnostics:
                    report["execution_detail"]["error_chain"] = exception_chain_codes(exc)
            trials.append(trial)
            report["model_requests_started"] += stats.requests_started
            report["results"].append({**asdict(trial), "passed": trial.passed,
                                       "model_requests_started": stats.requests_started,
                                       "seconds": round(time.monotonic() - call_started, 3)})
            if args.diagnostics:
                report["results"][-1]["steps"] = [asdict(step) for step in stats.steps]
            if "execution_failure" in report:
                break
        if "execution_failure" in report:
            break
    try:
        report["model_digest_unchanged"] = (
            await release_model_digest(MODEL) == digest
            and await loaded_release_model_digest(MODEL) == digest
        )
    except ModelDigestError:
        report["model_digest_unchanged"] = False
    report["gate"] = gate(trials, repeat=args.repeat)
    if not report["model_digest_unchanged"] or "execution_failure" in report:
        report["gate"]["passed"] = False
        report["gate"]["failures"].append("execution_or_provenance_failed")
    return report, 0 if report["gate"]["passed"] else 2


def main(argv: list[str] | None = None) -> int:
    report, code = asyncio.run(run(parse_args(sys.argv[1:] if argv is None else argv)))
    print(json.dumps(report, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
