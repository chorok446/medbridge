#!/usr/bin/env python
"""로컬 Q&A 품질 평가 실행기 (Sprint 4C-B).

실제 서비스 경로(검색→provider→스트리밍→검증→저장)를 통과시켜 로컬 모델을 평가하고
JSON·Markdown 보고서를 생성한다. 실제 Ollama 평가는 opt-in(수동)이며, 일반 CI는
deterministic 모드만 쓴다(모델 다운로드 없음).

예:
  uv run python scripts/evaluate_local_qa.py --model qwen3:8b
  uv run python scripts/evaluate_local_qa.py --model qwen3:8b --repeat 3 --fail-on-gate
  uv run python scripts/evaluate_local_qa.py --provider deterministic --category grounded_basic

--model은 allowlist(qwen3:4b/8b/14b)만 허용한다. Ollama·모델 미설치면 명확히 skip한다
(자동 다운로드하지 않는다). 임시 DB·파일은 실행 후 정리한다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
_DATASET_DIR = _API_ROOT / "tests" / "fixtures" / "qa_evaluation"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="로컬 Q&A 품질 평가")
    p.add_argument("--model", default="qwen3:8b", help="평가 모델(allowlist만)")
    p.add_argument("--provider", choices=["local", "deterministic"], default="local",
                   help="local=실제 Ollama, deterministic=파이프라인 검증")
    p.add_argument("--repeat", type=int, default=1, help="케이스별 반복(출시 평가는 3+)")
    p.add_argument("--category", default=None, help="특정 카테고리만")
    p.add_argument("--output-dir", default="artifacts/qa-evaluation")
    p.add_argument("--timeout", type=float, default=120.0, help="케이스당 timeout(초)")
    p.add_argument("--fail-on-gate", action="store_true",
                   help="안전·모델 게이트 실패 또는 미평가(skip) 시 비정상 종료")
    p.add_argument("--allow-skip", action="store_true",
                   help="--fail-on-gate에서도 Ollama/모델 미설치 skip을 성공으로 허용(탐색용)")
    p.add_argument("--keep-failed-artifacts", action="store_true",
                   help="임시 DB·파일을 지우지 않고 남긴다(디버깅)")
    return p.parse_args(argv)


# 종료 코드: 0 성공, 2 게이트/사용오류, 3 미평가(전제 미충족 skip)
EXIT_OK = 0
EXIT_GATE = 2
EXIT_NOT_EVALUATED = 3


async def _run(args: argparse.Namespace) -> int:
    # env를 먼저 세운 뒤 app 모듈을 임포트해야 임시 DB로 향한다.
    from alembic.config import Config as AlembicConfig

    from alembic import command
    from app.db.session import get_session_factory
    from app.qa_eval import manifest, ollama
    from app.qa_eval.report import write_reports
    from app.qa_eval.runner import run_evaluation
    from app.services.summary import secrets

    secrets.use_in_memory_backend()  # 평가는 OS 자격증명 저장소를 건드리지 않는다

    # 게이트 판정은 전체 데이터셋 실행에서만 유효하다 — 필터링된 실행으로 게이트를
    # 통과시킬 수 없게 막는다(안전 스위트 우회 방지).
    if args.fail_on_gate and args.category:
        print("[오류] --fail-on-gate는 카테고리 필터와 함께 쓸 수 없습니다(전체 데이터셋 필요).",
              file=sys.stderr)
        return EXIT_GATE

    cfg = AlembicConfig(str(_API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    if args.provider == "local":
        try:
            ready, reason = ollama.preflight(args.model)
        except ollama.ModelNotAllowedError as exc:
            print(f"[오류] {exc}", file=sys.stderr)
            return EXIT_GATE
        if not ready:
            print(f"[skip] {reason}")
            # --fail-on-gate에서는 '미평가'를 성공으로 보지 않는다(fail-open 방지).
            if args.fail_on_gate and not args.allow_skip:
                return EXIT_NOT_EVALUATED
            return EXIT_OK
        model_label = args.model
        model = args.model
    else:
        model_label = "deterministic"
        model = None

    ds = manifest.load_dataset(_DATASET_DIR)
    categories = [args.category] if args.category else None
    summary, gate, _ = await run_evaluation(
        get_session_factory(), ds,
        model_label=model_label, provider_mode=args.provider, model=model,
        repeat=args.repeat, categories=categories, timeout_sec=args.timeout,
    )
    generated_at = _now_iso()
    json_path, md_path = write_reports(
        args.output_dir, summary, gate, generated_at=generated_at
    )
    print(f"모델: {summary.model}")
    print(f"판정: {gate.verdict} (안전 {'통과' if gate.safety_passed else '실패'}, "
          f"모델게이트 {'통과' if gate.model_gate_passed else '실패'})")
    print(f"안전 위반 케이스: {summary.safety_failure_cases}, 변동: {summary.unstable_cases}")
    print(f"보고서: {json_path}  {md_path}")

    if args.fail_on_gate and not (gate.safety_passed and gate.model_gate_passed):
        return EXIT_GATE
    return EXIT_OK


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    tmp = tempfile.mkdtemp(prefix="medbridge-qaeval-")
    os.environ["MEDBRIDGE_APP_DATA_DIR"] = tmp  # app 임포트 전에 임시 DB 경로 주입
    try:
        return asyncio.run(_run(args))
    finally:
        if args.keep_failed_artifacts:
            print(f"[보존] 임시 데이터: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
