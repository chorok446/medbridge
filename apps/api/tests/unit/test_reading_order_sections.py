"""번호가 분리 추출된 절 제목과 선행 본문의 순서 경계를 검증한다."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.services.extraction.engine import BlockRec, LineRec, PageData
from app.services.extraction.pipeline import _prepare_page_rows
from app.services.extraction.reading_order import compute_reading_order
from app.services.search.chunking import build_chunk_drafts


def _block(index, bbox, text, block_type="text"):
    return BlockRec(bbox=bbox, text=text, block_index=index, block_type=block_type)


def _section_blocks(heading="2. \n장비 등급"):
    return [
        _block(0, (30, 100, 320, 112), "Previous table header"),
        _block(1, (35, 125, 130, 140), "Previous left A"),
        _block(2, (235, 125, 315, 140), "Previous right A"),
        _block(3, (35, 155, 130, 178), "Previous left B"),
        _block(4, (180, 155, 315, 182), "Previous right B"),
        _block(5, (32, 205, 135, 215), heading),
        _block(6, (40, 230, 310, 242), "A. Equipment with the basic parts."),
        _block(7, (40, 247, 310, 259), "B. Equipment with additional parts."),
    ]


_EXPECTED_ORDER = [0, 1, 3, 2, 4, 5, 6, 7]


@pytest.mark.parametrize("heading", [
    "2. \n장비 등급", "12.\nEquipment grades", "2.\r\n장비 등급",
    " 2.\t\n  장비 등급  ",
])
def test_standalone_numbered_heading_follows_both_previous_columns(heading):
    blocks = _section_blocks(heading)
    original = deepcopy(blocks)
    result = compute_reading_order(blocks, 350, 540)
    assert result.two_column
    assert result.order == _EXPECTED_ORDER
    assert result.confidence <= 0.5
    assert blocks == original


@pytest.mark.parametrize("heading", [
    "2. 장비 등급", "2)\n장비 등급", "II.\n장비 등급", "2\n장비 등급",
    "2.\n장비를 준비한다.", "2.\nEquipment grades?", "2.\n장비 등급\n세부 사항",
    "2. 설명\n장비 등급", "2.\n" + "장비" * 40, "2.\n", "장비 등급",
])
def test_unsupported_heading_keeps_column_order(heading):
    result = compute_reading_order(_section_blocks(heading), 350, 540)
    assert result.order.index(5) < result.order.index(2)


@pytest.mark.parametrize("bbox", [
    (180, 155, 315, 201),  # 제목 위에 충분한 간격이 없는 목록
    (180, 155, 315, 210),  # 제목 경계를 가로지르는 오른쪽 본문
    (235, 205, 315, 214),  # 같은 높이의 독립 오른쪽 본문
])
def test_no_clear_vertical_gap_keeps_column_order(bbox):
    blocks = _section_blocks()
    blocks[4].bbox = bbox
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


@pytest.mark.parametrize("bbox", [
    (40, 230, 135, 242),  # 가까운 좁은 본문 뒤의 전폭 블록으로 승격하지 않는다.
    (40, 260, 310, 272),  # 전폭 본문도 40pt보다 멀면 근거가 아니다.
])
def test_immediate_full_width_body_is_required(bbox):
    blocks = _section_blocks()
    blocks[6].bbox = bbox
    blocks[7].bbox = (40, 280, 310, 292)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


def test_all_nearest_body_blocks_must_be_full_width():
    blocks = _section_blocks()
    blocks.append(_block(8, (235, 230, 315, 242), "Independent right column"))
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


def test_body_overlapping_the_heading_is_not_a_standalone_section():
    blocks = _section_blocks()
    blocks.append(_block(8, (40, 211, 135, 223), "Overlapping body text"))
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


@pytest.mark.parametrize("gap", [9.99, 10.0])
def test_space_above_heading_requires_at_least_its_height(gap):
    blocks = _section_blocks()
    blocks[4].bbox = (180, 155, 315, 205 - gap)
    result = compute_reading_order(blocks, 350, 540)
    assert (result.order.index(5) > result.order.index(2)) is (gap >= 10.0)


def test_zero_height_heading_is_not_used_as_a_boundary():
    blocks = _section_blocks()
    blocks[5].bbox = (32, 205, 135, 205)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


def test_indented_heading_keeps_column_order():
    blocks = _section_blocks()
    blocks[5].bbox = (80, 205, 160, 215)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


def test_full_width_footnote_does_not_justify_a_section_boundary():
    blocks = _section_blocks()
    for block in blocks[5:]:
        x0, y0, x1, y1 = block.bbox
        block.bbox = (x0, y0 + 240, x1, y1 + 240)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(2)


def test_multiple_sections_and_nontext_preserve_order_and_original_indices():
    blocks = _section_blocks()
    following = deepcopy(blocks)
    for block in following:
        block.block_index += 8
        x0, y0, x1, y1 = block.bbox
        block.bbox = (x0, y0 + 190, x1, y1 + 190)
    all_blocks = blocks + following + [_block(16, (0, 0, 350, 540), "", "image")]
    result = compute_reading_order(list(reversed(all_blocks)), 350, 540)
    assert result.order == _EXPECTED_ORDER + [i + 8 for i in _EXPECTED_ORDER] + [16]


def test_prepared_rows_and_chunk_refs_preserve_the_corrected_order():
    blocks = _section_blocks()
    for block in blocks:
        block.lines = [LineRec(bbox=block.bbox, text=block.text, line_index=0)]
    raw = "\n".join(b.text for b in blocks)
    page = PageData(
        page_number=1, width=350, height=540, rotation=0, raw_text=raw,
        blocks=blocks, words=[], tables=[], image_bboxes=[], image_area_ratio=0,
        full_page_image=False, has_images=False, text_area_ratio=0.3,
    )
    prepared = _prepare_page_rows(uuid4(), page)
    positions = {index: pos for pos, index in enumerate(_EXPECTED_ORDER)}
    assert prepared.page_row.raw_text == raw
    assert not prepared.requires_ocr
    assert [b.reading_order for b in prepared.block_rows] == [positions[i] for i in range(8)]
    for original, row, line in zip(blocks, prepared.block_rows, prepared.line_rows, strict=True):
        assert row.text == line.text == original.text
        assert row.block_index == original.block_index
        assert (row.x0, row.y0, row.x1, row.y1) == original.bbox
        assert (line.x0, line.y0, line.x1, line.y1) == original.bbox
        assert line.reading_order == row.reading_order
    normalized = prepared.page_row.normalized_text
    assert normalized.index(blocks[4].text) < normalized.index("장비 등급")
    plan = build_chunk_drafts([prepared.page_row], prepared.block_rows)
    by_id = {b.id: b for b in prepared.block_rows}
    refs = [ref for draft in plan.drafts for ref in draft.refs]
    assert [by_id[ref.block_id].block_index for ref in refs] == _EXPECTED_ORDER
    for ref in refs:
        original = blocks[by_id[ref.block_id].block_index]
        assert ref.bbox == original.bbox
        assert ref.reading_order == positions[original.block_index]
        assert ref.page_number == 1 and ref.source_method == "digital"
