"""인용 도입 경계를 넘겨 주제·극성을 합치지 않는다(합성 비의료 예제)."""

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.schema import claims_conflict, verify
from tests.unit.test_qa_schema import _lookup

AXES = [
    "장치는 크기에 따라 소형(소형 장치)과 대형(대형 장치)으로 분류됩니다 "
    "(원문: small device, large device).",
    "장치는 상태에 따라 대기형(경고 없는 장치)과 작동형으로 분류됩니다 "
    "(원문: idle device has no warning, active device).",
]
MIXED = [
    "장치의 경고는 없다고 설명한다 (원문: thermal sensor warning is absent).",
    "장치의 구성은 센서와 외함이다 (원문: thermal sensor housing).",
]
POSITIVE = "시험에서 필터는 오염 입자를 제거한다"
NEGATIVE = "시험에서 필터는 오염 입자를 제거하지 않는다"


@pytest.mark.parametrize("texts", [AXES, MIXED])
@pytest.mark.parametrize("reverse", [False, True])
def test_unrelated_assertions_do_not_borrow_quote_topics(texts, reverse):
    assert not claims_conflict(list(reversed(texts)) if reverse else texts)


@pytest.mark.parametrize("label", ["원문", "인용", "출처", "original", "SOURCE", "quote"])
def test_source_introduction_is_not_a_shared_subject(label):
    texts = [t.replace("원문", label) for t in MIXED]
    assert not claims_conflict(texts)


@pytest.mark.parametrize("wrapper", ["(원문: {})", "（원문：{}）", "[source: {}]"])
def test_conflicting_quoted_evidence_is_still_compared(wrapper):
    assert claims_conflict([wrapper.format(POSITIVE), wrapper.format(NEGATIVE)])
    assert claims_conflict([POSITIVE, "보고서의 설명 " + wrapper.format(NEGATIVE)])


@pytest.mark.parametrize("texts", [
    [POSITIVE, NEGATIVE],
    ["필터는 (공기 중 오염 입자)를 제거한다",
     "필터는 (공기 중 오염 입자)를 제거하지 않는다"],
    ["장치는 색상에 따라 유형을 분류한다", "장치는 색상에 따라 유형을 분류하지 않는다"],
    [f"{POSITIVE} (원문: 기기에는 경고가 없다)", NEGATIVE],
    [f"설명 (원문: {POSITIVE}) (인용: 기기에는 경고가 없다)", NEGATIVE],
])
def test_real_conflicts_survive_inline_terms_and_unrelated_negative_quotes(texts):
    assert claims_conflict(texts)


def test_linking_words_alone_do_not_establish_a_subject():
    assert not claims_conflict([t.split(" (원문:")[0] for t in AXES])
    assert not claims_conflict([
        "Devices are grouped according to size",
        "Devices are not grouped according to state",
    ])


def test_shared_topic_variants_count_once_without_losing_one_sided_base():
    from app.services.qa.schema import _shared_subject_count

    assert _shared_subject_count({"장치", "장치는"}, {"장치", "장치는"}) == 1
    assert _shared_subject_count({"장치", "장치는"}, {"장치는"}) == 1
    assert _shared_subject_count({"필터", "필터는", "경고"}, {"필터", "필터는", "경고"}) == 2
    assert _shared_subject_count({"장치는", "필터는", "경고"}, {"장치는", "필터는", "경고"}) == 3


@pytest.mark.parametrize("texts", [AXES, MIXED])
@pytest.mark.parametrize("hint", ["answered", "conflicting_evidence"])
def test_batch_status_keeps_explicit_conflict_hint_and_source_refs(texts, hint):
    lookup = _lookup(*[(f"c{i}", text) for i, text in enumerate(texts)])
    result = verify({
        "answerStatus": hint,
        "claims": [{"text": text, "sourceChunkIds": [f"c{i}"]}
                   for i, text in enumerate(texts)],
    }, lookup, had_results=True)
    expected = "completed" if hint == "answered" else hint
    assert result.answer_status == expected
    assert [c.text for c in result.claims] == texts
    status = (QaClaimVerification.SUPPORTED if expected == "completed"
              else QaClaimVerification.CONFLICTING)
    assert all(c.verification_status == status for c in result.claims)
    assert [c.source_refs[0]["pageNumber"] for c in result.claims] == [1, 2]
