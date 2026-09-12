"""검증된 절 경계만 청크에 전달하고 출처와 기존 본문 규칙을 보존한다."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.models.enums import BlockType
from app.services.extraction import pipeline
from app.services.extraction.pipeline import _prepare_page_rows
from app.services.extraction.reading_order import compute_reading_order
from app.services.search.chunking import _ChunkDraftStream, build_chunk_drafts
from tests.extraction_fixtures import numbered_section_page


def _prepared(page_number=1):
    return _prepare_page_rows(uuid4(), numbered_section_page(page_number))


def test_validated_boundary_is_propagated_without_changing_source_data():
    page = numbered_section_page()
    original = deepcopy(page)
    order = compute_reading_order(page.blocks, page.width, page.height)
    assert order.section_heading_indices == frozenset({5})
    prepared = _prepare_page_rows(uuid4(), page)
    assert page == original
    for source, row in zip(page.blocks, prepared.block_rows, strict=True):
        assert row.text == source.text
        assert row.block_index == source.block_index
        assert (row.x0, row.y0, row.x1, row.y1) == source.bbox
        assert row.metadata_json == {
            "two_column": True,
            **({"section_boundary": True} if row.block_index == 5 else {}),
        }
    plan = build_chunk_drafts([prepared.page_row], prepared.block_rows)
    assert len(plan.drafts) == 2
    assert [r.block_id for r in plan.drafts[1].refs] == [
        row.id for row in prepared.block_rows[5:]
    ]
    assert plan.drafts[1].section_title == page.blocks[5].text
    assert plan.drafts[1].text_parts == [b.text for b in page.blocks[5:]]
    by_id = {b.id: b for b in prepared.block_rows}
    refs = [r for d in plan.drafts for r in d.refs]
    assert [by_id[r.block_id].block_index for r in refs] == [0, 1, 3, 2, 4, 5, 6, 7]
    for ref in refs:
        row = by_id[ref.block_id]
        assert ref.bbox == (row.x0, row.y0, row.x1, row.y1)
        assert ref.reading_order == row.reading_order
        assert ref.page_number == 1 and ref.source_method == "digital"


def test_boundary_is_recorded_when_legacy_order_already_matches():
    page = numbered_section_page()
    # 전폭 블록이 이전 2단을 닫으면 새 경계로 순서를 바꿀 필요가 없어도 표시한다.
    page.blocks[4].bbox = (30, 185, 320, 195)
    result = compute_reading_order(page.blocks, page.width, page.height)
    assert result.order == [0, 1, 3, 2, 4, 5, 6, 7]
    assert result.confidence == 0.85
    assert result.section_heading_indices == frozenset({5})


@pytest.mark.parametrize("marker", ["_mark_caption_blocks", "_mark_table_blocks"])
def test_pipeline_does_not_mark_blocks_already_classified_as_tables_or_captions(
    monkeypatch, marker,
):
    monkeypatch.setattr(pipeline, marker, lambda page: {5})
    prepared = _prepared()
    assert "section_boundary" not in prepared.block_rows[5].metadata_json


def test_empty_page_has_no_section_indices():
    assert compute_reading_order([], 350, 540).section_heading_indices == frozenset()


@pytest.mark.parametrize("case", [
    "ordinary_multiline", "caption", "indented", "gap", "overlap", "far_body",
    "narrow_body", "single_column", "image", "footnote",
])
def test_unvalidated_headings_do_not_get_section_metadata(case):
    page = numbered_section_page()
    heading = page.blocks[5]
    if case == "ordinary_multiline":
        heading.text = "장비 설명\n추가 설명"
    elif case == "caption":
        heading.text = "Table 2.\nEquipment grades"
    elif case == "indented":
        heading.bbox = (80, 205, 160, 215)
    elif case == "gap":
        page.blocks[4].bbox = (180, 155, 315, 201)
    elif case == "overlap":
        page.blocks[4].bbox = (180, 155, 315, 210)
    elif case == "far_body":
        page.blocks[6].bbox = (40, 260, 310, 272)
        page.blocks[7].bbox = (40, 280, 310, 292)
    elif case == "narrow_body":
        page.blocks[6].bbox = (40, 230, 135, 242)
    elif case == "single_column":
        page.blocks = [b for b in page.blocks if b.block_index not in (2, 4)]
    elif case == "image":
        heading.block_type = "image"
    elif case == "footnote":
        for block in page.blocks[5:]:
            x0, y0, x1, y1 = block.bbox
            block.bbox = (x0, y0 + 240, x1, y1 + 240)
    result = compute_reading_order(page.blocks, page.width, page.height)
    assert not result.section_heading_indices
    prepared = _prepare_page_rows(uuid4(), page)
    assert all("section_boundary" not in row.metadata_json for row in prepared.block_rows)


@pytest.mark.parametrize("metadata", [None, {}, {"section_boundary": False},
    {"section_boundary": "true"}, {"section_boundary": 1}])
def test_legacy_and_non_boolean_metadata_keep_multiline_body_behavior(metadata):
    prepared = _prepared()
    prepared.block_rows[5].metadata_json = metadata
    plan = build_chunk_drafts([prepared.page_row], prepared.block_rows)
    assert len(plan.drafts) == 1
    assert plan.drafts[0].section_title is None
    assert len(plan.drafts[0].refs) == 8


@pytest.mark.parametrize("case", ["ocr", "image", "caption", "table", "header", "footer"])
def test_boundary_flag_does_not_override_non_digital_text_or_exclusions(case):
    prepared = _prepared()
    heading = prepared.block_rows[5]
    heading.metadata_json = {"section_boundary": True}
    if case == "ocr":
        # 전부 OCR인 페이지도 시험해 디지털 우선 제외 규칙에 의존하지 않는다.
        for row in prepared.block_rows:
            row.metadata_json = {**row.metadata_json, "source": "ocr"}
    elif case in ("image", "caption"):
        heading.block_type = BlockType(case)
    else:
        setattr(heading, f"is_{case}", True)
    plan = build_chunk_drafts([prepared.page_row], prepared.block_rows)
    assert all(d.section_title is None for d in plan.drafts)
    if case == "table":
        assert [d.is_table for d in plan.drafts] == [False, True, False]
        assert plan.drafts[1].refs[0].block_id == heading.id
    elif case in ("header", "footer"):
        assert all(r.block_id != heading.id for d in plan.drafts for r in d.refs)
    else:
        assert len(plan.drafts) == 1


@pytest.mark.parametrize("split_batches", [False, True])
def test_identical_short_sections_never_merge_across_explicit_boundaries(split_batches):
    first, second = _prepared(1), _prepared(2)
    # 짧고 동일한 제목을 두 번 사용해 제목 문자열 비교에 의한 재병합을 검출한다.
    rows_a, rows_b = first.block_rows[5:7], second.block_rows[5:7]
    for row in (rows_a[0], rows_b[0]):
        row.metadata_json = {"section_boundary": True}
    stream = _ChunkDraftStream()
    if split_batches:
        drafts = stream.consume([first.page_row], rows_a)
        drafts += stream.consume([second.page_row], rows_b)
    else:
        drafts = stream.consume([first.page_row, second.page_row], rows_a + rows_b)
    drafts += stream.finish()
    assert len(drafts) == 2
    assert [d.section_title for d in drafts] == [rows_a[0].text] * 2
    assert [[r.block_id for r in d.refs] for d in drafts] == [
        [b.id for b in rows_a], [b.id for b in rows_b],
    ]
