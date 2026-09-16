"""분절된 순수 원문 인용만 출처를 완성하고 기존 검증은 유지한다."""

import copy

import pytest

from app.services.qa.context import QaChunkRef
from app.services.qa.schema import classify_claim_event, verify

FRAGMENTS = [
    "예제 장치는 보호기능（자동제",
    "어）으로 작동하며 외부 전원이 끊기면",
    "저장된 신호를 전송하지 못한다.\n다음 표의 값은 999이다.",
]
QUOTE = (
    "예제 장치는 보호기능(자동제어)으로 작동하며 외부 전원이 끊기면 "
    "저장된 신호를 전송하지 못한다."
)


def sources(fragments=FRAGMENTS):
    return {
        f"c{i}": QaChunkRef(f"c{i}", None, text, f"hash-{i}", [{
            "pageNumber": 5, "readingOrder": i, "blockId": f"b{i}",
            "bbox": [0, i * 10, 100, (i + 1) * 10],
        }])
        for i, text in enumerate(fragments)
    }


def check(path, quote, ids, lookup):
    event = {"text": quote, "sourceChunkIds": ids}
    if path == "stream":
        claim, _ = classify_claim_event(event, lookup, claim_index=0)
        return claim
    answer = verify({"answer": quote, "answerStatus": "answered", "claims": [event]},
                    lookup, had_results=True)
    return answer.claims[0] if answer.answer_status == "completed" else None


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("ids", [["c0", "c1"], ["c1"], ["c0", "c1", "c2"]])
def test_complete_exact_quote_keeps_text_and_original_provenance(path, ids):
    lookup = sources()
    before = copy.deepcopy(lookup)
    claim = check(path, QUOTE, ids, lookup)
    assert claim is not None
    assert claim.text == QUOTE
    assert claim.source_chunk_ids == ["c0", "c1", "c2"]
    assert [ref["blockId"] for ref in claim.source_refs] == ["b0", "b1", "b2"]
    assert lookup == before


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("change", ["other_page", "gap", "ambiguous", "missing_order"])
def test_do_not_join_uncertain_or_disjoint_source_positions(path, change):
    lookup = sources()
    if change == "other_page":
        lookup["c2"].source_refs[0]["pageNumber"] = 6
    elif change == "gap":
        lookup["c2"].source_refs[0]["readingOrder"] = 9
    elif change == "missing_order":
        lookup["c2"].source_refs[0].pop("readingOrder")
    else:
        lookup["duplicate"] = copy.deepcopy(lookup["c2"])
        lookup["duplicate"].chunk_id = "duplicate"
    assert check(path, QUOTE, ["c0", "c1"], lookup) is None


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("text", [
    QUOTE + " (원문: 전송하지 못한다.)",  # 추가 설명/중복 인용을 버려 맞추지 않는다.
    QUOTE.replace("못한다", "않는다"),
    QUOTE.replace("저장된 신호", "123개의 저장된 신호"),
    "환자의 혈압은 999이다. " + QUOTE,
])
def test_do_not_repair_rewritten_or_added_assertions(path, text):
    assert check(path, text, ["c0", "c1"], sources()) is None


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_no_source_or_unrelated_source_is_not_filled_in(path):
    lookup = sources()
    lookup["other"] = QaChunkRef("other", None, "다른 주제", "other-hash", [])
    assert check(path, QUOTE, [], lookup) is None
    assert check(path, QUOTE, ["other"], lookup) is None


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_only_chunks_crossed_by_the_quote_are_added(path):
    lookup = sources([
        "이 기록의 첫 문장이다.", "예제 장치는 정상 신호를 보내며",
        "결과를 표시한다.", "전혀 다른 기록이다.",
    ])
    claim = check(path, "예제 장치는 정상 신호를 보내며 결과를 표시한다.", ["c1"], lookup)
    assert claim is not None
    assert claim.source_chunk_ids == ["c1", "c2"]


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_do_not_join_split_digits_into_a_new_value(path):
    lookup = sources(["값은 1", "0이며 정상이다."])
    assert check(path, "값은 10이며 정상이다.", ["c0"], lookup) is None


@pytest.mark.parametrize("path", ["stream", "batch"])
def test_unknown_id_is_not_repaired_using_other_known_ids(path):
    assert check(path, QUOTE, ["c0", "c1", "unknown"], sources()) is None


def test_do_not_repair_more_than_four_chunks_or_modify_truncated_claims():
    from app.services.qa.literal_sources import complete_literal_source_ids

    lookup = sources(["합성 원문 조각" + str(i) for i in range(5)])
    text = " ".join(ref.text for ref in lookup.values())
    assert complete_literal_source_ids(text, ["c0"], lookup) == ["c0"]
    assert complete_literal_source_ids(QUOTE + " " * 600, ["c0"], sources()) == ["c0"]


def test_do_not_complete_from_text_outside_model_visible_prefix():
    from app.services.qa.literal_sources import complete_literal_source_ids
    from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS

    lookup = sources(["x" * CHUNK_TEXT_MAX_CHARS + " 숨겨진 원문은", "예제 신호를 보내지 못한다."])
    quote = "숨겨진 원문은 예제 신호를 보내지 못한다."
    assert complete_literal_source_ids(quote, ["c1"], lookup) == ["c1"]
