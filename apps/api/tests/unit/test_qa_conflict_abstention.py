"""상충 힌트에서 한쪽 근거만 검증된 답변은 완료로 승격하지 않는다."""

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.schema import classify_claim_event, verify
from app.services.qa.stream_service import _final_status
from tests.unit.test_qa_schema import _lookup

FIRST = "초기 연구에서는 이 요법이 사망 위험을 감소시킨다고 보고하였다"
SECOND = "후속 연구에서는 이 요법이 사망 위험에 영향을 주지 않았다고 보고하였다"


@pytest.mark.parametrize("path", ["batch", "stream"])
@pytest.mark.parametrize(("bad_claim", "reason"), [
    ({"text": SECOND, "sourceChunkIds": ["unknown"]}, "no_valid_source"),
    ({"text": SECOND}, "no_valid_source"),
    ({"text": "", "sourceChunkIds": ["c2"]}, "empty_text"),
    ({"text": SECOND + " 999명", "sourceChunkIds": ["c2"]}, "number_not_in_source"),
    ({"text": "간은 담즙을 분비하고 해독을 담당한다", "sourceChunkIds": ["c2"]},
     "not_lexically_grounded"),
])
def test_rejected_counterpart_does_not_turn_conflict_into_completed(path, bad_claim, reason):
    lookup = _lookup(("c1", FIRST), ("c2", SECOND))
    good = {"text": FIRST, "sourceChunkIds": ["c1"]}
    rejected, actual_reason = classify_claim_event(bad_claim, lookup, claim_index=1)
    assert rejected is None
    assert actual_reason == reason  # 잘못된 ID·수치·어휘를 복구하지 않는다.
    if path == "batch":
        result = verify({
            "answer": FIRST + "[c0]", "answerStatus": "conflicting_evidence",
            "claims": [good, bad_claim], "followUpSuggestions": ["후속 연구는?"],
        }, lookup, had_results=True)
        assert result.answer_status == "insufficient_evidence"
        assert result.claims == []
        assert result.followups == []
        body = result.answer
    else:
        supported, error = classify_claim_event(good, lookup, claim_index=0)
        assert supported is not None and error is None
        status, body = _final_status([supported], "conflicting_evidence", had_results=True)
        assert status.value == "insufficient_evidence"
    assert FIRST not in body
    assert "[c0]" not in body


@pytest.mark.parametrize("path", ["batch", "stream"])
@pytest.mark.parametrize(("hint", "count", "had_results", "expected"), [
    ("conflicting_evidence", 0, True, "insufficient_evidence"),
    ("conflicting_evidence", 1, True, "insufficient_evidence"),
    ("conflicting_evidence", 2, True, "conflicting_evidence"),
    ("answered", 1, True, "completed"),
    ("answered", 2, True, "conflicting_evidence"),
    ("not_found", 1, True, "not_found"),
    ("conflicting_evidence", 1, False, "not_found"),
    ("insufficient_evidence", 2, True, "insufficient_evidence"),
])
def test_final_status_preserves_supported_answers_and_abstention_precedence(
    path, hint, count, had_results, expected,
):
    lookup = _lookup(("c1", FIRST), ("c2", SECOND))
    claims = [{"text": FIRST, "sourceChunkIds": ["c1"]},
              {"text": SECOND, "sourceChunkIds": ["c2"]}][:count]
    if path == "batch":
        result = verify({"answerStatus": hint, "claims": claims}, lookup,
                        had_results=had_results)
        assert result.answer_status == expected
        if expected == "conflicting_evidence":
            assert len(result.claims) == 2
            assert all(c.verification_status == QaClaimVerification.CONFLICTING
                       for c in result.claims)
    else:
        supported = [classify_claim_event(c, lookup, claim_index=i)[0]
                     for i, c in enumerate(claims)]
        assert all(c is not None for c in supported)
        status, _ = _final_status(supported, hint, had_results=had_results)
        assert status.value == expected
