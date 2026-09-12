"""정의 요청에서 질문의 제목만 반복한 비답변을 양쪽 경로에서 거부한다."""

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.schema import classify_claim_event, verify

QUESTION = "예제 장치의 정의를 설명하는 원문 한 문장과 출처를 보여주세요."
DEFINITION = "예제 장치는 저장된 신호를 전송하는 기기이다."


def source(text="06. 예제 장치", title=None):
    return {"c0": QaChunkRef("c0", title, text, "hash", [{
        "pageNumber": 1, "readingOrder": 0, "blockId": "b0", "bbox": [0, 0, 100, 10],
    }])}


def accepted(path, text, question=QUESTION, lookup=None):
    lookup = source() if lookup is None else lookup
    event = {"text": text, "sourceChunkIds": ["c0"]}
    if path == "stream":
        claim, reason = classify_claim_event(event, lookup, claim_index=0, question=question)
        return claim is not None, reason
    answer = verify({"answer": text + "[c0]", "answerStatus": "answered", "claims": [event]},
                    lookup, had_results=True, question=question)
    return answer.answer_status == "completed", None


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("text", [
    "예제 장치", "06. 예제 장치", "출처: 예제 장치 (원문: 예제 장치)",
    '원문: “예제 장치”', "예제 장치 [c0]",
])
def test_definition_cannot_be_completed_with_just_the_heading(path, text):
    ok, reason = accepted(path, text)
    assert not ok
    if path == "stream":
        assert reason == "definition_heading_only"


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("question", ["예제 장치의 의미는?", "예제 장치의 뜻을 알려줘"])
def test_explicit_meaning_requests_are_also_protected(path, question):
    assert not accepted(path, "예제 장치", question)[0]


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_heading_metadata_is_checked_even_when_chunk_contains_body(path):
    assert not accepted(path, "예제 장치", lookup=source(DEFINITION, "예제 장치"))[0]


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_single_word_heading_with_repeated_source_metadata(path):
    assert not accepted(path, "출처: 가상장치 (원문: 가상장치)", "가상장치의 정의는?",
                        source("06. 가상장치"))[0]


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_english_definition_heading_is_not_an_answer(path):
    assert not accepted(path, "Example device", "What is the definition of Example device?",
                        source("06. Example device"))[0]


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("question", [
    "목차에서 예제 장치 제목을 보여줘", "예제 장치 정의 부분의 제목은?", "",
])
def test_title_queries_and_legacy_calls_keep_short_answers(path, question):
    assert accepted(path, "예제 장치", question)[0]


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_short_definition_and_acronym_expansion_remain_valid(path):
    assert accepted(path, DEFINITION, lookup=source(DEFINITION, "예제 장치"))[0]
    assert accepted(path, "Heart Failure", "HF의 의미를 알려줘",
                    source("HF: Heart Failure", "Heart Failure"))[0]
    assert accepted(path, "상수", "계수의 뜻은?", source("계수: 상수", "상수"))[0]


def test_batch_marks_only_heading_unsupported_in_mixed_answer():
    lookup = source(DEFINITION, "예제 장치")
    output = {"answerStatus": "answered", "answer": "예제 장치[c0] " + DEFINITION + "[c1]",
              "claims": [{"text": text, "sourceChunkIds": ["c0"]}
                         for text in ("예제 장치", DEFINITION)]}
    answer = verify(output, lookup, had_results=True, question=QUESTION)
    assert answer.answer_status == "completed"
    assert [claim.verification_status for claim in answer.claims] == [
        QaClaimVerification.UNSUPPORTED, QaClaimVerification.SUPPORTED,
    ]
    assert "[c0]" not in answer.answer
    assert "[c1]" in answer.answer
