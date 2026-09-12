#!/usr/bin/env python
"""로컬 Q&A 품질 평가 실행기 (Sprint 4C-B).

실제 서비스 경로(검색→provider→스트리밍→검증→저장)를 통과시켜 로컬 모델을 평가하고
JSON·Markdown 보고서를 생성한다. 실제 Ollama 평가는 opt-in(수동)이며, 일반 CI는
deterministic 모드만 쓴다(모델 다운로드 없음).

예:
  uv run python scripts/evaluate_local_qa.py --model qwen3:8b
  uv run python scripts/evaluate_local_qa.py --model qwen3:8b --repeat 3 --fail-on-gate
  uv run python scripts/evaluate_local_qa.py --provider deterministic --category grounded_basic

--model은 allowlist(qwen3:4b/8b/14b/30b-a3b)만 허용한다. Ollama·모델 미설치면 명확히 skip한다
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
_REPO_ROOT = _API_ROOT.parents[1]
_DATASET_DIR = _API_ROOT / "tests" / "fixtures" / "qa_evaluation"


class ReleaseEvaluationError(RuntimeError):
    """출시 평가 환경·실행 정체성을 신뢰할 수 없음."""


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
    args = p.parse_args(argv)
    if args.repeat < 1:
        p.error("--repeat는 1 이상이어야 합니다.")
    if args.fail_on_gate:
        if args.provider != "local":
            p.error("출시 평가는 --provider local만 허용합니다.")
        if args.model != "qwen3:8b":
            p.error("출시 평가는 --model qwen3:8b만 허용합니다.")
        if args.repeat < 3:
            p.error("출시 평가는 --repeat 3 이상이어야 합니다.")
        if args.category:
            p.error("출시 평가는 카테고리 필터 없이 전체 데이터셋을 실행해야 합니다.")
        if args.allow_skip:
            p.error("출시 평가에서는 --allow-skip을 사용할 수 없습니다.")
    return args


def _is_release_evaluation(args: argparse.Namespace) -> bool:
    """_parse_args가 완전한 출시 조합으로 검증한 --fail-on-gate 실행."""
    return bool(args.fail_on_gate)


def _configure_evaluation_environment(tmp: str) -> str:
    """사용자 DB·.env override보다 우선하는 평가 전용 환경을 강제한다."""
    app_data = Path(tmp).resolve()
    database_url = f"sqlite+aiosqlite:///{(app_data / 'medbridge.db').as_posix()}"
    os.environ["MEDBRIDGE_APP_DATA_DIR"] = str(app_data)
    os.environ["DATABASE_URL"] = database_url
    os.environ["QA_PROVIDER"] = "auto"
    # 검색 결과가 사용자 .env의 테스트 embedding 설정에 좌우되지 않게 기본 경로로 고정한다.
    os.environ["EMBEDDING_PROVIDER"] = "disabled"
    return database_url


def _verify_loaded_settings(settings, expected_database_url: str) -> None:
    if settings.database_url != expected_database_url:
        raise ReleaseEvaluationError("평가 DATABASE_URL이 임시 DB와 일치하지 않습니다.")
    if settings.qa_provider != "auto":
        raise ReleaseEvaluationError("평가 QA_PROVIDER가 auto로 고정되지 않았습니다.")


# 종료 코드: 0 성공, 2 게이트/사용오류, 3 미평가(전제 미충족 skip)
EXIT_OK = 0
EXIT_GATE = 2
EXIT_NOT_EVALUATED = 3


async def _run(
    args: argparse.Namespace,
    *,
    expected_database_url: str,
    initial_git_snapshot=None,
) -> int:
    # env를 먼저 세운 뒤 app 모듈을 임포트해야 임시 DB로 향한다.
    from alembic.config import Config as AlembicConfig

    from alembic import command
    from app.core.config import get_settings
    from app.db.session import get_session_factory
    from app.qa_eval import manifest, ollama
    from app.qa_eval.provenance import ReleaseGuard
    from app.qa_eval.report import ReleaseProvenance, write_reports
    from app.qa_eval.run_case import materialize_and_verify_local_runtime
    from app.qa_eval.runner import run_evaluation
    from app.services.local_ai import settings as local_st
    from app.services.summary import secrets

    _verify_loaded_settings(get_settings(), expected_database_url)

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
            ready, reason = await ollama.preflight(args.model)
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

    release_guard = None
    if _is_release_evaluation(args):
        # DB row를 먼저 실제 값으로 만든 다음 factory가 내는 provider 정체성을 검증한다.
        await materialize_and_verify_local_runtime(get_session_factory(), model="qwen3:8b")
        release_guard = await ReleaseGuard.create(
            _REPO_ROOT,
            initial_snapshot=initial_git_snapshot,
        )
        release_guard.verify_repository()
        await release_guard.verify_digest("evaluation-start")

    ds = manifest.load_dataset(_DATASET_DIR)
    categories = [args.category] if args.category else None
    summary, gate, _ = await run_evaluation(
        get_session_factory(), ds,
        model_label=model_label, provider_mode=args.provider, model=model,
        repeat=args.repeat, categories=categories, timeout_sec=args.timeout,
        run_boundary=release_guard.run_boundary if release_guard is not None else None,
    )
    report_provenance = None
    if release_guard is not None:
        await release_guard.verify_digest("evaluation-end")
        release_guard.verify_repository()
        report_provenance = ReleaseProvenance(
            tested_commit=release_guard.snapshot.tested_commit,
            model_digest=release_guard.model_digest,
            endpoint=local_st.OLLAMA_OPENAI_BASE,
        )
    generated_at = _now_iso()
    json_path, md_path = write_reports(
        args.output_dir,
        summary,
        gate,
        generated_at=generated_at,
        provenance=report_provenance,
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
    expected_database_url = _configure_evaluation_environment(tmp)
    try:
        initial_git_snapshot = None
        if _is_release_evaluation(args):
            # 환경을 강제한 뒤 app 설정을 import하기 전에 평가할 commit과 clean tree를 고정한다.
            from app.qa_eval.provenance import capture_git_snapshot

            initial_git_snapshot = capture_git_snapshot(_REPO_ROOT)
        return asyncio.run(
            _run(
                args,
                expected_database_url=expected_database_url,
                initial_git_snapshot=initial_git_snapshot,
            )
        )
    except Exception as exc:
        if not _is_release_evaluation(args):
            raise
        # 출시 경로는 어떤 사전조건·provenance 오류도 artifact 없이 fail-closed한다.
        print(f"[오류] 출시 평가 검증에 실패했습니다: {exc}", file=sys.stderr)
        return EXIT_GATE
    finally:
        if args.keep_failed_artifacts:
            print(f"[보존] 임시 데이터: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
