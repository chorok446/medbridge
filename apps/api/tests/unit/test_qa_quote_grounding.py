"""원문 인용이 같은 언어의 근거 없는 설명을 세탁하지 못하게 한다."""

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.schema import REJECT_NOT_GROUNDED, classify_claim_event, verify

SOURCE = "심장은 온몸에 혈액을 보내는 근육 기관이다."
BLOOD = "'혈액'은 심장이 보내는 유체로, 산소와 영양분을 운반하는 역할을 합니다"
MUSCLE = (
    "'근육'은 심장이 근육 조직으로 이루어져 있으며, "
    "수축과 이완을 통해 혈액을 펌프하는 특성을 나타냅니다"
)


def _check(text, source, path):
    lookup = {"c1": QaChunkRef("c1", None, source, "hash", [])}
    event = {"text": text, "sourceChunkIds": ["c1"]}
    if path == "stream":
        claim, reason = classify_claim_event(event, lookup, claim_index=0)
        if claim is None:
            assert reason == REJECT_NOT_GROUNDED
        return claim is not None
    result = verify(
        {"answer": text, "answerStatus": "answered", "claims": [event]},
        lookup,
        had_results=True,
    )
    return any(c.verification_status == QaClaimVerification.SUPPORTED for c in result.claims)


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("assertion", [BLOOD, MUSCLE])
@pytest.mark.parametrize("quote", [
    f"(원문: {SOURCE})",
    f"[출처: {SOURCE}]",
    f"（인용：{SOURCE}）",
    f'"{SOURCE}"',
    f"“{SOURCE}”",
])
def test_source_quote_does_not_support_added_explanation(path, assertion, quote):
    assert not _check(f"{assertion} {quote}.", SOURCE, path)


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_multiple_quotes_do_not_hide_unsupported_explanation(path):
    text = f"(원문: {SOURCE}) {BLOOD} (원문: {SOURCE})."
    assert not _check(text, SOURCE, path)


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_repeated_unquoted_evidence_does_not_support_added_explanation(path):
    # 실제 모델 재검증에서 발견: 같은 원문을 괄호 밖에도 반복해 어휘 비율을 채운다.
    text = (
        "심장은 온몸에 혈액을 보내는 근육 기관이다라는 문장에서 "
        "'심장'은 혈액을 펌프하는 주요 기관을 가리키는 단어이다 "
        f"(원문: {SOURCE})."
    )
    assert not _check(text, SOURCE, path)


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_english_quote_does_not_support_added_explanation(path):
    source = "The library opens at nine on weekdays."
    text = f"Students receive free meals and unlimited printing (source: {source})."
    assert not _check(text, source, path)


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("text", [
    SOURCE,
    f"(원문: {SOURCE})",
    f'"{SOURCE}"',
    f"{SOURCE} (원문: {SOURCE})",
    f"심장은 혈액을 보내는 기관입니다 (원문: {SOURCE}).",
    "문단 1. 심장은 온몸에 혈액을 보내는 근육 기관이다.",
])
def test_direct_quotes_and_grounded_paraphrases_remain_supported(path, text):
    assert _check(text, f"문단 1. {SOURCE}", path)


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_explanation_is_allowed_when_it_is_in_the_source(path):
    assert _check(f"{BLOOD} (원문: {SOURCE}).", f"{SOURCE} {BLOOD}.", path)


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_existing_cross_language_contract_is_preserved(path):
    assert _check(
        "도서관은 평일 오전 9시에 엽니다 (원문: library opens at 9 on weekdays).",
        "The library opens at 9 on weekdays.",
        path,
    )
