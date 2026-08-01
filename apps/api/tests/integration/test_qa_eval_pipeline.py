"""Layer 1 — 평가 프레임워크가 실제 서비스 경로를 통과하는지 (deterministic provider).

실제 모델 없이 grounded_basic·not_found 카테고리를 end-to-end로 돌려 러너·씨딩·검증·
집계·게이트가 동작하는지 확인한다. (의미 카테고리의 정오는 실제 qwen3 평가에서 판정.)
"""

from pathlib import Path

from app.db.session import get_session_factory
from app.qa_eval import manifest
from app.qa_eval.runner import run_evaluation

DATASET = Path(__file__).resolve().parents[1] / "fixtures" / "qa_evaluation"


async def test_grounded_case_passes_through_real_path():
    ds = manifest.load_dataset(DATASET)
    summary, gate, per_case = await run_evaluation(
        get_session_factory(), ds,
        model_label="deterministic", provider_mode="deterministic",
        categories=["grounded_basic"], repeat=1, timeout_sec=60,
    )
    assert per_case, "grounded_basic 케이스가 있어야 한다"
    for results in per_case:
        r = results[0]
        assert r.checks["protocol"], r.reason
        assert r.status == "completed", f"{r.case_id}: {r.status}"
        assert r.checks["ownership"], "출처는 이 문서 소유여야 한다"
        assert r.claim_count >= 1
        assert not r.safety_violations
        assert r.passed, f"{r.case_id} 전체 통과 기대: {r.reason}"
    assert summary.safety_failure_cases == 0
    assert gate.safety_passed


async def test_not_found_holds_answer():
    # 키워드 겹침이 전혀 없는 '완전 부재' 질문은 deterministic에서도 검색 결과가 없어
    # not_found가 되어야 한다. ('관련 단어만 있는' 케이스의 보류는 실제 모델에서 판정.)
    ds = manifest.load_dataset(DATASET)
    _summary, _gate, per_case = await run_evaluation(
        get_session_factory(), ds,
        model_label="deterministic", provider_mode="deterministic",
        categories=["not_found"], repeat=1, timeout_sec=60,
    )
    by_id = {results[0].case_id: results[0] for results in per_case}
    absent = by_id["not_found_absent"]
    assert absent.status in ("not_found", "insufficient_evidence"), absent.status
    assert absent.claim_count == 0
    assert not absent.safety_violations


async def test_summary_and_gate_shape():
    ds = manifest.load_dataset(DATASET)
    summary, gate, _ = await run_evaluation(
        get_session_factory(), ds,
        model_label="deterministic", provider_mode="deterministic",
        categories=["grounded_basic", "not_found"], repeat=2, timeout_sec=60,
    )
    assert summary.total_runs == summary.total_cases * 2
    assert 0.0 <= summary.protocol_success_rate <= 1.0
    assert gate.verdict  # 판정 문자열이 존재
