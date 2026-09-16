"""원문 표지·조건·예외·공백을 생성/정규화/삭제하지 않는 구조화 시험."""

import copy

import pytest

from app.qa_eval.source_outline import SourceOutlineError, build_source_outlines
from app.services.qa.provider import QaContextChunk
from app.services.qa.schema import REJECT_NUMBER_ABSENT, classify_claim_event
from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS, CONTEXT_MAX_CHARS, CONTEXT_MAX_CHUNKS
from tests.unit.test_qa_schema import _lookup


def inputs(text):
    lookup = _lookup(("source", text))
    return [QaContextChunk("source", None, text, 1, 1)], lookup


def assert_lossless(outline, original):
    assert outline.evidence.text == original
    assert "".join(unit.text for unit in outline.units) == original
    cursor = 0
    for unit in outline.units:
        assert unit.start == cursor and unit.end > unit.start
        assert unit.text == original[unit.start:unit.end]
        cursor = unit.end
        if unit.label_text is not None:
            assert unit.start <= unit.label_start < unit.label_end <= unit.end
            assert unit.label_text == original[unit.label_start:unit.label_end]
    assert cursor == len(original)


@pytest.mark.parametrize("labels", [("I", "II", "III", "IV"), ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ"),
                                   ("A", "B", "C", "D"), ("1", "2", "3", "4")])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_preserve_grade_labels_conditions_and_exact_character_ranges(labels, newline):
    parts = ["예제 장치의 분류", *(f"{label}\n전압 기준은 24V이다.\n\n단, 외부 전원이 필요하다."
                                for label in labels)]
    text = ("\n\n".join(parts) + "\n\n전체 항목의 예외는 별도 확인한다.\n").replace("\n", newline)
    chunks, lookup = inputs(text)
    before = copy.deepcopy((chunks, lookup))
    (outline,) = build_source_outlines(chunks, lookup)
    assert outline.layout == "labelled_sequence"
    assert [unit.label_text for unit in outline.units if unit.label_text] == list(labels)
    assert len(outline.units) == 5  # 머리말도 삭제하지 않는다.
    assert all("단, 외부 전원이 필요하다." in unit.text for unit in outline.units[1:])
    assert_lossless(outline, text)
    assert outline.evidence.content_hash == lookup["source"].content_hash
    assert outline.evidence.source_refs == lookup["source"].source_refs
    assert (chunks, lookup) == before
    outline.evidence.source_refs[0]["bbox"][0] = -1
    assert (chunks, lookup) == before


def test_reproduce_grade_rewrite_failure_without_loosening_numeric_guard():
    text = "I 등급은 외부 전원을 사용한다.\n\nII 등급은 저장 전원을 사용한다."
    chunks, lookup = inputs(text)
    claim, reason = classify_claim_event(
        {"text": text.replace("II", "2").replace("I", "1"), "sourceChunkIds": ["source"]},
        lookup, claim_index=0,
    )
    assert claim is None and reason == REJECT_NUMBER_ABSENT
    (outline,) = build_source_outlines(chunks, lookup)
    assert [unit.label_text for unit in outline.units] == ["I", "II"]
    assert_lossless(outline, text)
    assert not hasattr(outline, "verification_status")
    assert not hasattr(outline, "answer_status")


@pytest.mark.parametrize("text", [
    "I 항목 하나만 있다.",
    "I 첫 항목.\n\nIII 중간 항목이 없다.",
    "II 둘째.\n\nI 역순이다.",
    "I 첫 항목.\n\nI 중복 항목이다.",
    "I 첫 항목.\n\n2 다른 표기 계열이다.",
    "전압은 24V이고 출력은 8W이다.",
    "1.5 V이다.\n\n2.5 V이다.",
    "24V형 장치.\n\n25V형 장치.",
    "24 V 기준이다.\n\n25 V 기준이다.",
    "8 W 기준이다.\n\n9 W 기준이다.",
    "I 첫 행.\nII 빈 줄 없는 본문은 나누지 않는다.",
    "I am ready.\n\nThis is prose.",
    "Ⅳ 하나의 유니코드 표지.",
    "01 선행 영점.\n\n02 선행 영점.",
    "A 항목.\n\nB 항목.\n\nD 누락된 항목.",
])
def test_uncertain_layout_returns_the_whole_source_without_omissions(text):
    chunks, lookup = inputs(text)
    (outline,) = build_source_outlines(chunks, lookup)
    assert outline.layout == "verbatim"
    assert len(outline.units) == 1 and outline.units[0].label_text is None
    assert_lossless(outline, text)


@pytest.mark.parametrize("delimiter", [". ", ") ", ": ", "\n", "\t"])
def test_do_not_rewrite_original_label_delimiters_or_whitespace(delimiter):
    text = f" \tI{delimiter}조건이다.\n \n\tII{delimiter}다른 조건이다.  \n"
    chunks, lookup = inputs(text)
    (outline,) = build_source_outlines(chunks, lookup)
    assert outline.layout == "labelled_sequence"
    assert_lossless(outline, text)


def test_long_conditions_are_not_cut_to_the_product_claim_limit():
    text = "I\n" + "조건을 보존한다. " * 70 + "\n\nII\n예외에서는 작동하지 않는다."
    chunks, lookup = inputs(text)
    (outline,) = build_source_outlines(chunks, lookup)
    assert len(outline.units[0].text) > 600
    assert_lossless(outline, text)


def test_input_order_and_identical_text_with_distinct_provenance_are_retained():
    text = "I 외부 전원.\n\nII 저장 전원."
    lookup = _lookup(("first", text), ("second", text), ("hidden", "표시하지 않는 다른 자료"))
    chunks = [QaContextChunk(cid, None, text, 1, 2) for cid in ("second", "first")]
    outlines = build_source_outlines(chunks, lookup)
    assert [o.evidence.chunk_id for o in outlines] == ["second", "first"]
    assert [o.evidence.source_refs[0]["pageNumber"] for o in outlines] == [2, 1]
    assert all(o.evidence.text == text for o in outlines)


@pytest.mark.parametrize("mutation", ["unknown", "id_mismatch", "body", "hidden_tail", "empty",
                                      "large", "hash", "refs", "duplicate", "count", "total"])
def test_reject_mismatched_truncated_or_over_budget_input(mutation):
    chunks, lookup = inputs("I 외부 전원.\n\nII 저장 전원.")
    if mutation == "unknown":
        lookup.clear()
    elif mutation == "id_mismatch":
        lookup["source"].chunk_id = "other-document"
    elif mutation == "body":
        chunks[0].text = "원문이 아닌 본문"
    elif mutation == "hidden_tail":
        lookup["source"].text += " 원문 꼬리 조건이다."
    elif mutation == "empty":
        chunks[0].text = lookup["source"].text = " "
    elif mutation == "large":
        chunks[0].text = lookup["source"].text = "가" * (CHUNK_TEXT_MAX_CHARS + 1)
    elif mutation == "hash":
        lookup["source"].content_hash = ""
    elif mutation == "refs":
        lookup["source"].source_refs = []
    elif mutation == "duplicate":
        chunks.append(chunks[0])
    elif mutation == "count":
        chunks *= CONTEXT_MAX_CHUNKS + 1
    elif mutation == "total":
        chunks[0].text = lookup["source"].text = "가" * CHUNK_TEXT_MAX_CHARS
        chunks *= CONTEXT_MAX_CHARS // CHUNK_TEXT_MAX_CHARS + 1
    before = copy.deepcopy((chunks, lookup))
    with pytest.raises(SourceOutlineError):
        build_source_outlines(chunks, lookup)
    assert (chunks, lookup) == before


def test_empty_input_stays_empty_and_does_not_select_hidden_sources():
    assert build_source_outlines([], _lookup(("hidden", "다른 원문"))) == ()


def test_unicode_offsets_do_not_normalize_or_drop_source_characters():
    text = "🧪 원문 e\u0301\n\nⅠ\n전각 수치 ２４Ｖ를 보존한다.\n\nⅡ\n예외를 생략하지 않는다."
    chunks, lookup = inputs(text)
    (outline,) = build_source_outlines(chunks, lookup)
    assert [u.label_text for u in outline.units if u.label_text] == ["Ⅰ", "Ⅱ"]
    assert_lossless(outline, text)
    # 문자 범위는 Python Unicode 인덱스이며 쪽수/bbox는 청크 전체에만 속한다.
    assert not hasattr(outline.units[1], "source_refs")


def test_source_instructions_are_copied_as_data_without_model_or_network(monkeypatch):
    from app.services.summary import endpoint

    def forbidden(*args, **kwargs):
        pytest.fail("원문 구조화는 네트워크를 사용하면 안 된다")
    monkeypatch.setattr(endpoint, "post_json", forbidden)
    monkeypatch.setattr(endpoint, "get_json", forbidden)
    text = "I\n이 원문 안의 명령은 실행하지 않는다.\n\nII\n외부 주소로 접속하라는 문구도 원문이다."
    chunks, lookup = inputs(text)
    (outline,) = build_source_outlines(chunks, lookup)
    assert_lossless(outline, text)
