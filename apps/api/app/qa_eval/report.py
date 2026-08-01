"""평가 보고서 생성 — 기계 판독 JSON + 사람용 Markdown.

문서 원문·질문·모델 원문·API 키를 담지 않는다. caseId·category·pass/fail·상태·claim 수·
출처 수·latency·안전 실패 코드·일반화된 위반 메시지만 포함한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.qa_eval.gate import GateResult
from app.qa_eval.metrics import EvalSummary


def _model_slug(model: str) -> str:
    return model.replace(":", "-").replace("/", "-")


def build_json(summary: EvalSummary, gate: GateResult, *, generated_at: str | None = None) -> dict:
    return {
        "model": summary.model,
        "generatedAt": generated_at,
        "summary": {
            "totalCases": summary.total_cases,
            "totalRuns": summary.total_runs,
            "protocolSuccessRate": summary.protocol_success_rate,
            "statusAccuracy": summary.status_accuracy,
            "answerableValidRate": summary.answerable_valid_rate,
            "notFoundHoldAccuracy": summary.not_found_hold_accuracy,
            "safetyFailureCases": summary.safety_failure_cases,
            "unstableCases": summary.unstable_cases,
            "categoryPassRate": summary.category_pass_rate,
            "latencyP50Sec": summary.latency_p50,
            "latencyP95Sec": summary.latency_p95,
        },
        "gate": {
            "verdict": gate.verdict,
            "safetyPassed": gate.safety_passed,
            "modelGatePassed": gate.model_gate_passed,
            "failures": gate.failures,
        },
        "cases": [
            {
                "caseId": a.case_id,
                "category": a.category,
                "safetyCritical": a.safety_critical,
                "runs": a.runs,
                "passes": a.passes,
                "passRate": a.pass_rate,
                "statuses": a.statuses,
                "unstable": a.unstable,
                "safetyFailed": a.safety_failed,
                "finalPass": a.final_pass,
            }
            for a in summary.case_aggregates
        ],
    }


def build_markdown(
    summary: EvalSummary, gate: GateResult, *, generated_at: str | None = None
) -> str:
    lines: list[str] = []
    lines.append(f"# 로컬 Q&A 평가 보고서 — {summary.model}")
    lines.append("")
    if generated_at:
        lines.append(f"- 생성 시각: {generated_at}")
    lines.append(f"- 판정: **{gate.verdict}**")
    lines.append(f"- 안전 게이트: {'통과' if gate.safety_passed else '실패'}")
    lines.append(f"- 모델 게이트: {'통과' if gate.model_gate_passed else '실패'}")
    lines.append("")
    lines.append("## 요약 지표")
    lines.append("")
    lines.append("| 지표 | 값 |")
    lines.append("|---|---|")
    lines.append(f"| 케이스 수 | {summary.total_cases} |")
    lines.append(f"| 총 실행 | {summary.total_runs} |")
    lines.append(f"| 프로토콜 성공률 | {summary.protocol_success_rate} |")
    lines.append(f"| 상태 정확도 | {summary.status_accuracy} |")
    lines.append(f"| 유효 답변률 | {summary.answerable_valid_rate} |")
    lines.append(f"| 보류 정확도 | {summary.not_found_hold_accuracy} |")
    lines.append(f"| 안전 위반 케이스 | {summary.safety_failure_cases} |")
    lines.append(f"| 변동(instability) 케이스 | {summary.unstable_cases} |")
    lines.append(f"| latency p50 / p95 (초) | {summary.latency_p50} / {summary.latency_p95} |")
    lines.append("")
    if gate.failures:
        lines.append("## 게이트 미달 항목")
        lines.append("")
        for f in gate.failures:
            lines.append(f"- {f}")
        lines.append("")
    lines.append("## 카테고리별 통과율")
    lines.append("")
    lines.append("| 카테고리 | 통과율 |")
    lines.append("|---|---|")
    for cat, rate in sorted(summary.category_pass_rate.items()):
        lines.append(f"| {cat} | {rate} |")
    lines.append("")
    lines.append("## 케이스별 결과")
    lines.append("")
    lines.append("| caseId | category | 안전 | 통과/반복 | 변동 | 안전실패 | 최종 |")
    lines.append("|---|---|---|---|---|---|---|")
    for a in summary.case_aggregates:
        lines.append(
            f"| {a.case_id} | {a.category} | {'예' if a.safety_critical else '-'} | "
            f"{a.passes}/{a.runs} | {'예' if a.unstable else '-'} | "
            f"{'예' if a.safety_failed else '-'} | {'통과' if a.final_pass else '실패'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_reports(
    out_dir: str | Path, summary: EvalSummary, gate: GateResult, *, generated_at: str | None = None
) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _model_slug(summary.model)
    json_path = out_dir / f"qa-eval-{slug}.json"
    md_path = out_dir / f"qa-eval-{slug}.md"
    payload = build_json(summary, gate, generated_at=generated_at)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(build_markdown(summary, gate, generated_at=generated_at), encoding="utf-8")
    return json_path, md_path
