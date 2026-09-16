"""잘못 붙인 실제 ID는 전체 청크 원문이 일의적으로 일치할 때만 복구한다."""

import copy

import pytest

from app.services.qa.literal_sources import complete_literal_source_ids
from app.services.qa.schema import REJECT_NUMBER_ABSENT, classify_claim_event
from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS, CLAIM_TEXT_MAX_CHARS
from tests.unit.test_qa_literal_sources import check
from tests.unit.test_qa_schema import _lookup

TEXT = "예제 장치의 전압 분류 기준은 24V이며 정격 상태에서만 적용한다."
OTHER = "예제 장치의 출력 분류 기준은 8W이며 절전 상태에서만 적용한다."


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("whitespace", [False, True])
def test_rebind_only_the_whole_literal_chunk_and_preserve_provenance(path, whitespace):
    source = TEXT.replace(" ", "\n") if whitespace else TEXT
    lookup = _lookup(("voltage", source), ("power", OTHER))
    before = copy.deepcopy(lookup)
    claim = check(path, TEXT, ["power"], lookup)
    assert claim is not None
    assert claim.text == TEXT
    assert claim.source_chunk_ids == ["voltage"]
    assert claim.source_refs == before["voltage"].source_refs
    assert lookup == before


@pytest.mark.parametrize("change", [
    "missing_id", "unknown_id", "mixed_unknown_id", "duplicate_chunk",
    "embedded_duplicate", "partial_quote", "source_has_tail", "unseen_tail",
    "rewritten", "unit_changed", "added_assertion", "negation_changed",
    "wrapper", "case_changed", "missing_order", "missing_page", "ref_id_mismatch",
    "two_pages", "order_gap", "short", "long", "hidden_quote",
])
def test_uncertain_or_nonliteral_binding_is_left_unchanged(change):
    lookup = _lookup(("voltage", TEXT), ("power", OTHER))
    ids, text = ["power"], TEXT
    if change == "missing_id":
        ids = []
    elif change == "unknown_id":
        ids = ["unknown"]
    elif change == "mixed_unknown_id":
        ids += ["unknown"]
    elif change in {"duplicate_chunk", "embedded_duplicate"}:
        duplicate = copy.deepcopy(lookup["voltage"])
        duplicate.chunk_id = "duplicate"
        if change == "embedded_duplicate":
            duplicate.text = "다른 장치의 기록. " + TEXT + " 추가 조건은 별도다."
        lookup["duplicate"] = duplicate
    elif change == "partial_quote":
        text = TEXT.removesuffix(" 정격 상태에서만 적용한다.")
    elif change == "source_has_tail":
        lookup["voltage"].text += " 다만 외부 전원은 예외다."
    elif change == "unseen_tail":
        lookup["voltage"].text += " " * CHUNK_TEXT_MAX_CHARS + "단, 예외가 있다."
    elif change == "rewritten":
        text = TEXT.replace("기준은", "기준값은")
    elif change == "unit_changed":
        text = TEXT.replace("24V", "24W")
    elif change == "added_assertion":
        text += " 장치의 무게는 24kg이다."
    elif change == "negation_changed":
        text = TEXT.replace("적용한다", "적용하지 않는다")
    elif change == "wrapper":
        text = '"' + TEXT + '"'
    elif change == "case_changed":
        text = TEXT.replace("24V", "24v")
    elif change == "missing_order":
        lookup["voltage"].source_refs[0].pop("readingOrder")
    elif change == "missing_page":
        lookup["voltage"].source_refs[0].pop("pageNumber")
    elif change == "ref_id_mismatch":
        lookup["voltage"].chunk_id = "other-document"
    elif change in {"two_pages", "order_gap"}:
        extra = copy.deepcopy(lookup["voltage"].source_refs[0])
        extra["pageNumber" if change == "two_pages" else "readingOrder"] = 9
        lookup["voltage"].source_refs.append(extra)
    elif change == "short":
        text = lookup["voltage"].text = "장치 전압은 24V이다."
    elif change == "long":
        text = lookup["voltage"].text = TEXT + " " * CLAIM_TEXT_MAX_CHARS
    elif change == "hidden_quote":
        lookup["voltage"].text = "x" * CHUNK_TEXT_MAX_CHARS + TEXT
    before = copy.deepcopy(lookup)
    assert complete_literal_source_ids(text, ids, lookup) == ids
    assert lookup == before


def test_rebinding_does_not_override_the_existing_numeric_guard():
    # NFKC 비교는 전각 숫자를 같게 보지만 제품 수치 검사는 더 엄격하다.
    lookup = _lookup(("voltage", TEXT.replace("24V", "２４Ｖ")), ("power", OTHER))
    assert complete_literal_source_ids(TEXT, ["power"], lookup) == ["voltage"]
    claim, reason = classify_claim_event(
        {"text": TEXT, "sourceChunkIds": ["power"]}, lookup, claim_index=0,
    )
    assert claim is None
    assert reason == REJECT_NUMBER_ABSENT


def test_already_cited_literal_source_is_not_replaced_or_reordered():
    lookup = _lookup(("voltage", TEXT), ("power", OTHER))
    ids = ["power", "voltage"]
    assert complete_literal_source_ids(TEXT, ids, lookup) == ids


@pytest.mark.parametrize("ambiguous_position", [False, True])
def test_alternative_spanning_quote_prevents_whole_chunk_rebinding(ambiguous_position):
    lookup = _lookup(("voltage", TEXT), ("power", OTHER),
                     ("part1", "예제 장치의 전압 분류 기준은 24V이며"),
                     ("part2", "정격 상태에서만 적용한다."))
    lookup["part2"].source_refs[0]["pageNumber"] = 3
    if ambiguous_position:
        duplicate = copy.deepcopy(lookup["part1"])
        duplicate.chunk_id = "duplicate"
        lookup["duplicate"] = duplicate
    assert complete_literal_source_ids(TEXT, ["power"], lookup) == ["power"]
