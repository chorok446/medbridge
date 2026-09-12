"""검색 전체에 있는 숫자로 잘못 인용한 청크의 근거를 대신하지 않는다."""

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.schema import REJECT_NUMBER_ABSENT, classify_claim_event, verify
from tests.unit.test_qa_schema import _lookup

VOLTAGE = "장치의 전압 기준은 24V이다."
POWER = "장치의 출력 기준은 8W이다."


@pytest.mark.parametrize("path", ["batch", "stream"])
@pytest.mark.parametrize(("claim_text", "source_ids", "supported"), [
    (VOLTAGE, ["following"], False),
    (VOLTAGE, ["voltage"], True),
    (VOLTAGE + " " + POWER, ["following"], False),
    (VOLTAGE + " " + POWER, ["voltage", "following"], True),
])
def test_classification_numbers_require_the_actually_cited_sources(
    path, claim_text, source_ids, supported,
):
    # 서로 다른 쪽의 청크: 연속된 순수 인용 ID 복원 대상이 아니다.
    lookup = _lookup(("voltage", VOLTAGE), ("following", POWER))
    event = {"text": claim_text, "sourceChunkIds": source_ids.copy()}
    original_sources = {cid: (ref.text, ref.content_hash, ref.source_refs.copy())
                        for cid, ref in lookup.items()}
    question = "장치의 분류"
    if path == "stream":
        claim, reason = classify_claim_event(event, lookup, claim_index=0, question=question)
        assert (claim is not None) is supported
        assert reason == (None if supported else REJECT_NUMBER_ABSENT)
        accepted = [claim] if claim else []
    else:
        answer = verify({"answerStatus": "answered", "claims": [event]}, lookup,
                        had_results=True, question=question)
        assert answer.answer_status == ("completed" if supported else "insufficient_evidence")
        accepted = [c for c in answer.claims
                    if c.verification_status == QaClaimVerification.SUPPORTED]
        assert bool(accepted) is supported
    for claim in accepted:
        assert claim.text == claim_text
        assert claim.source_chunk_ids == source_ids
        assert [r["pageNumber"] for r in claim.source_refs] == (
            [1, 2] if len(source_ids) == 2 else [1]
        )
    assert event == {"text": claim_text, "sourceChunkIds": source_ids}
    assert {cid: (ref.text, ref.content_hash, ref.source_refs)
            for cid, ref in lookup.items()} == original_sources
