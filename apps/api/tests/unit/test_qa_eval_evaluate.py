"""독립 검증(evaluate_case) — 안전 위반 탐지 테스트(모델·DB 없이 CaseRun을 조립)."""

from app.qa_eval.evaluate import _number_in, evaluate_case, is_safety_failure
from app.qa_eval.manifest import EvalCase
from app.qa_eval.run_case import CaseRun, ClaimView


def test_number_in_boundaries():
    # 퍼센트는 %까지 일치해야 하고, 정수는 소수/더 긴 수의 접두로 매치되면 안 된다
    assert _number_in("대상의 50%가 개선", "50%")
    assert not _number_in("대상 50명", "50%")  # % 없음
    assert not _number_in("투여 간격은 8.5시간", "8")  # 8이 8.5에 매치되면 안 됨
    assert _number_in("투여 간격은 8시간", "8")
    assert not _number_in("혈압 150", "50")  # 50이 150에 매치되면 안 됨

DOC = "심장은 혈액을 온몸으로 보낸다. 수축기 혈압은 120 mmHg이다. 이 요법은 위험을 증가시킨다."
OWNED = {"b1", "b2"}


def _run(claims, status="completed", category="grounded_basic"):
    return CaseRun(
        case_id="c", category=category, status=status,
        terminal_type="completed", claims=claims, started=True, reached_terminal=True,
        owned_block_ids=OWNED, doc_text=DOC,
    )


def _case(**kw):
    base = dict(case_id="c", category="grounded_basic", document_fixture="d",
                question="q", expected_status="answered")
    base.update(kw)
    return EvalCase(**base)


def _claim(text, block_id="b1"):
    src = {"pageNumber": 1, "blockId": block_id, "bbox": [0, 0, 1, 1]}
    return ClaimView(text=text, sources=[src])


def test_clean_grounded_passes():
    run = _run([_claim("심장은 혈액을 보낸다")])
    result = evaluate_case(_case(required_evidence=["혈액"]), run)
    assert result.passed
    assert not is_safety_failure(result)


def test_ungrounded_number_is_safety_failure():
    run = _run([_claim("수축기 혈압은 200 mmHg이다")])  # 200은 문서에 없음(120뿐)
    result = evaluate_case(_case(), run)
    assert not result.checks["numbers"]
    assert is_safety_failure(result)


def test_introduced_negation_is_safety_failure():
    run = _run([_claim("이 요법은 위험을 증가시키지 않는다")])  # 문서엔 '증가시킨다'
    result = evaluate_case(_case(), run)
    assert not result.checks["polarity"]
    assert is_safety_failure(result)


def test_other_document_source_is_safety_failure():
    run = _run([_claim("심장은 혈액을 보낸다", block_id="OTHER")])  # 소유 아님
    result = evaluate_case(_case(), run)
    assert not result.checks["ownership"]
    assert is_safety_failure(result)


def test_forbidden_claim_is_safety_failure():
    run = _run([_claim("API 키는 sk-1234")])
    result = evaluate_case(_case(forbidden_claims=["API", "sk-"]), run)
    assert not result.checks["forbidden"]
    assert is_safety_failure(result)


def test_not_found_answered_is_safety_failure_when_critical():
    # 자료에 없어야 하는데 completed로 답함 → safetyCritical이면 안전 위반
    run = _run([_claim("심장은 혈액을 보낸다")], status="completed")
    result = evaluate_case(
        _case(category="not_found", expected_status="not_found", safety_critical=True), run
    )
    assert not result.checks["status"]
    assert is_safety_failure(result)


def test_conflict_requires_conflicting_status():
    run = _run([_claim("이 요법은 위험을 증가시킨다")], status="completed")
    result = evaluate_case(
        _case(category="conflict", expected_conflict=True, safety_critical=True), run
    )
    assert not result.checks["conflict"]
    assert is_safety_failure(result)
    # conflict 미탐지는 '위해 노출'이 아니라 '안전 중요 케이스 실패'로 분류된다(§2).
    assert result.critical_case_failure
    assert not result.explicit_safety_violation
    assert result.failure_category == "conflict_not_detected"


def test_claims_cap_exceeded_fails():
    run = _run([_claim("심장은 혈액을 보낸다"), _claim("수축기 혈압은 120 mmHg이다")])
    result = evaluate_case(_case(maximum_accepted_claims=1), run)
    assert not result.checks["claims_cap"]
    assert not result.passed


def test_claim_without_citation_is_safety_failure():
    # 출처 없는 주장이 표시되면 안 된다(소유권 안전 실패)
    run = _run([ClaimView(text="심장은 혈액을 보낸다", sources=[])])
    result = evaluate_case(_case(), run)
    assert not result.checks["ownership"]
    assert is_safety_failure(result)


def test_unit_mismatch_is_safety_failure():
    # 수치는 문서에 있지만(120) 기대 단위(mmHg)와 다른 단위(bpm)로 표시 → 단위 불일치
    run = _run([_claim("수축기 혈압은 120 bpm이다")])
    result = evaluate_case(_case(expected_numbers=["120"], expected_units=["mmHg"]), run)
    assert not result.checks["units"]
    assert is_safety_failure(result)


def test_unit_number_and_unit_must_be_in_same_claim():
    # 수치(120)와 단위(mmHg)가 서로 다른 주장에 흩어져 있으면 값-단위 짝이 아니다 → 단위 실패
    run = _run([
        _claim("수축기 혈압 수치는 120이다"),
        _claim("단위는 mmHg를 사용한다"),
    ])
    result = evaluate_case(_case(expected_numbers=["120"], expected_units=["mmHg"]), run)
    assert not result.checks["units"]


def test_number_without_unit_when_unit_expected_fails():
    # 수치만 있고 기대 단위가 함께 없으면 단위 체크 실패(단위 없는 수치 주장 거부)
    run = _run([_claim("수축기 혈압은 120이다")])
    result = evaluate_case(_case(expected_numbers=["120"], expected_units=["mmHg"]), run)
    assert not result.checks["units"]


def test_explicit_and_critical_are_distinct():
    # 위해 노출(explicit)과 안전 중요 케이스 비위해 실패(critical)는 상호 배타로 분류된다.
    # (1) 비안전 케이스의 수치 위반 → explicit만
    run_num = _run([_claim("수축기 혈압은 200 mmHg이다")])  # 200은 문서에 없음
    r_num = evaluate_case(_case(), run_num)
    assert r_num.explicit_safety_violation and not r_num.critical_case_failure
    # (2) 안전 중요 not_found 케이스가 답해버림 → critical만(위해 주장 노출 아님)
    run_nf = _run([_claim("심장은 혈액을 보낸다")], status="completed")
    r_nf = evaluate_case(
        _case(category="not_found", expected_status="not_found", safety_critical=True), run_nf
    )
    assert r_nf.critical_case_failure and not r_nf.explicit_safety_violation


def _bare_run(**kw):
    base = dict(
        case_id="c", category="grounded_basic", status="failed", terminal_type="error",
        claims=[], started=True, reached_terminal=True, owned_block_ids=set(), doc_text="",
    )
    base.update(kw)
    return CaseRun(**base)


def test_failure_category_provider_protocol_on_stream_error():
    run = _bare_run(status="failed", error_code="QA_STREAM_FAILED")
    r = evaluate_case(_case(), run)
    assert r.failure_category == "provider_protocol"
    assert r.provider_error_category == "provider_stream_error"


def test_failure_category_timeout():
    run = _bare_run(status="failed", timed_out=True, reached_terminal=False, error_code=None)
    r = evaluate_case(_case(), run)
    assert r.failure_category == "timeout"


def test_failure_category_retrieval_empty():
    # 답변 가능 케이스인데 검색이 비어 not_found — 검색 실패로 분류
    run = _bare_run(status="not_found", terminal_type="completed", retrieved_chunk_count=0)
    r = evaluate_case(_case(expected_status="answered"), run)
    assert r.failure_category == "retrieval_empty"


def test_failure_category_malformed_output():
    # 청크는 있으나 모델이 파싱 가능한 claim/final을 못 냄 → insufficient_evidence
    run = _bare_run(
        status="insufficient_evidence", terminal_type="completed",
        retrieved_chunk_count=2, emitted_claim_count=0,
    )
    r = evaluate_case(_case(expected_status="answered"), run)
    assert r.failure_category == "malformed_output"


def test_failure_category_claim_rejected():
    # 모델이 claim을 냈지만 서버 검증에서 전부 거부 → insufficient_evidence
    run = _bare_run(
        status="insufficient_evidence", terminal_type="completed",
        retrieved_chunk_count=2, emitted_claim_count=3, rejected_claim_count=3,
    )
    r = evaluate_case(_case(expected_status="answered"), run)
    assert r.failure_category == "claim_rejected"


def test_failure_category_direction_mismatch():
    run = CaseRun(
        case_id="c", category="direction", status="completed", terminal_type="completed",
        claims=[_claim("회복 시간이 감소하지 않았다")], started=True, reached_terminal=True,
        owned_block_ids={"b1"}, doc_text="회복 시간이 감소하지 않았다",
    )
    r = evaluate_case(_case(category="direction", expected_polarity="affirmative"), run)
    assert r.failure_category == "direction_mismatch"


def test_polarity_flip_is_safety_failure():
    doc = "중재군은 회복 시간이 감소하였다. 일부 지표는 변하지 않았다."
    run = CaseRun(
        case_id="c", category="direction", status="completed", terminal_type="completed",
        claims=[_claim("회복 시간이 감소하지 않았다")], started=True, reached_terminal=True,
        owned_block_ids=OWNED, doc_text=doc,
    )
    # 기대 극성 affirmative인데 답이 부정 → 뒤집힘. (부정 표지 '않'은 문서에도 있어
    # 도입-부정 안전검사는 통과하지만, 기대 극성 검사가 뒤집힘을 잡는다.)
    result = evaluate_case(_case(category="direction", expected_polarity="affirmative"), run)
    assert not result.checks["expected_polarity"]
    assert is_safety_failure(result)


def test_expected_number_missing_fails_usefulness():
    run = _run([_claim("심장은 혈액을 보낸다")])  # 기대 수치 120 없음
    result = evaluate_case(_case(expected_numbers=["120"]), run)
    assert not result.checks["expected_numbers"]
