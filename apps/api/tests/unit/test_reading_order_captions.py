"""합성 좌표로 표 경계 오염과 보수적인 읽기 순서 보정을 검증한다."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.services.extraction.engine import BlockRec, LineRec, PageData
from app.services.extraction.pipeline import _prepare_page_rows
from app.services.extraction.reading_order import compute_reading_order
from app.services.search.chunking import build_chunk_drafts


def _block(index, bbox, text, block_type="text"):
    return BlockRec(bbox=bbox, text=text, block_index=index, block_type=block_type)


def caption_blocks(caption="표 2-9, 장비 등급"):
    # 이전 표의 두 열 뒤에 짧은 표 제목과 전폭 행이 이어진다.
    # 첫 행의 bbox는 캡션 하단과 겹치지만 캡션 위쪽을 가로지르지 않는다.
    return [
        _block(0, (30, 100, 320, 112), "Previous table header"),
        _block(1, (35, 125, 130, 140), "Previous left A"),
        _block(2, (35, 150, 130, 165), "Previous left B"),
        _block(3, (235, 125, 315, 140), "Previous right A"),
        _block(4, (235, 150, 315, 165), "Previous right B"),
        _block(5, (30, 190, 170, 200), caption),
        _block(6, (40, 197, 190, 218), "I Basic equipment."),
        _block(7, (40, 210, 310, 235), "II Additional equipment."),
        _block(8, (40, 230, 310, 250), "III Extended equipment."),
        _block(9, (40, 248, 210, 268), "IV Complete equipment."),
    ]


@pytest.mark.parametrize("caption", [
    "표 2-9, 장비 등급", "표 3. 장비 등급", "표12 장비 등급",
    "Table 2-9. Equipment grades", "TABLE 12 Equipment grades",
    "표 2-9.\n장비 등급",
])
def test_previous_table_stays_before_next_caption(caption):
    blocks = caption_blocks(caption)
    original = deepcopy(blocks)
    result = compute_reading_order(blocks, 350, 540)

    assert result.two_column
    assert result.order == list(range(10))
    assert result.confidence <= 0.5
    assert blocks == original  # 텍스트, 좌표, 원래 인덱스를 변경하지 않는다.


@pytest.mark.parametrize("caption", [
    "Equipment grades", "See Table 2 for equipment", "표본 2 장비 등급",
    "Table two Equipment grades", "Table 2a Equipment grades",
    "2. Equipment grades", "표 2 " + "장비 " * 40,
])
def test_unsupported_text_keeps_existing_column_order(caption):
    result = compute_reading_order(caption_blocks(caption), 350, 540)
    assert result.two_column
    assert result.order.index(5) < result.order.index(3)


def test_independent_column_caption_without_full_width_rows_is_not_a_boundary():
    blocks = caption_blocks()
    blocks[7].bbox = (40, 215, 155, 235)
    blocks[8].bbox = (40, 240, 155, 260)
    blocks[9].bbox = (40, 265, 155, 285)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(3)


@pytest.mark.parametrize("bbox", [
    (235, 180, 315, 205),  # 경계를 가로지르는 오른쪽 본문
    (235, 190, 315, 199),  # 같은 높이의 독립적인 오른쪽 본문
])
def test_conflicting_column_at_caption_height_keeps_existing_order(bbox):
    blocks = caption_blocks()
    blocks[4].bbox = bbox
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(3)


def test_distant_full_width_rows_do_not_support_caption_boundary():
    blocks = caption_blocks()
    for block in blocks[7:]:
        x0, y0, x1, y1 = block.bbox
        block.bbox = (x0, y0 + 100, x1, y1 + 100)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(3)


def test_indented_caption_is_not_a_page_boundary():
    blocks = caption_blocks()
    blocks[5].bbox = (80, 190, 170, 200)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order.index(5) < result.order.index(3)


def test_footnotes_and_nontext_keep_their_placement():
    blocks = caption_blocks() + [
        _block(10, (30, 480, 170, 490), "표 3 각주 예시"),
        _block(11, (235, 475, 315, 485), "Right footnote"),
        _block(12, (0, 0, 350, 540), "", "image"),
        _block(13, (0, 0, 0, 0), "   "),
    ]
    result = compute_reading_order(blocks, 350, 540)
    assert result.order[:10] == list(range(10))
    assert result.order[10:] == [11, 10, 12, 13]


def test_single_column_and_full_width_caption_are_unchanged():
    blocks = caption_blocks()
    blocks[5].bbox = (30, 190, 320, 200)
    blocks[6].bbox = (40, 201, 190, 218)
    result = compute_reading_order(blocks, 350, 540)
    assert result.order == list(range(10))
    single = [blocks[5], blocks[7], blocks[8]]
    result = compute_reading_order(single, 350, 540)
    assert not result.two_column
    assert result.order == [5, 7, 8]


def test_multiple_caption_boundaries_preserve_each_table_and_original_indices():
    blocks = caption_blocks()
    following = deepcopy(blocks)
    for block in following:
        block.block_index += 10
        x0, y0, x1, y1 = block.bbox
        block.bbox = (x0, y0 + 190, x1, y1 + 190)
    result = compute_reading_order(list(reversed(blocks + following)), 350, 600)
    assert result.order == list(range(20))


def test_full_width_footnote_cannot_justify_body_caption():
    blocks = caption_blocks()
    for block in blocks[5:]:
        x0, y0, x1, y1 = block.bbox
        block.bbox = (x0, y0 + 260, x1, y1 + 260)
    result = compute_reading_order(blocks, 350, 530)
    assert result.order.index(5) < result.order.index(3)


def test_pipeline_and_chunk_drafts_keep_caption_rows_and_source_coordinates():
    blocks = caption_blocks()
    for block in blocks:
        block.lines = [LineRec(bbox=block.bbox, text=block.text, line_index=0)]
    raw_text = "\n".join(b.text for b in blocks)
    page = PageData(
        page_number=1, width=350, height=540, rotation=0,
        raw_text=raw_text, blocks=blocks, words=[], tables=[],
        image_bboxes=[], image_area_ratio=0, full_page_image=False,
        has_images=False, text_area_ratio=0.3,
    )
    prepared = _prepare_page_rows(uuid4(), page)
    assert prepared.page_row.raw_text == raw_text
    assert not prepared.requires_ocr
    assert [b.reading_order for b in prepared.block_rows] == list(range(10))
    assert [line.reading_order for line in prepared.line_rows] == list(range(10))
    for original, row in zip(blocks, prepared.block_rows, strict=True):
        assert row.text == original.text
        assert row.block_index == original.block_index
        assert (row.x0, row.y0, row.x1, row.y1) == original.bbox
    normalized = prepared.page_row.normalized_text
    assert normalized.index(blocks[4].text) < normalized.index(blocks[5].text)

    plan = build_chunk_drafts([prepared.page_row], prepared.block_rows)
    by_id = {row.id: row for row in prepared.block_rows}
    refs = [ref for draft in plan.drafts for ref in draft.refs]
    assert [by_id[ref.block_id].block_index for ref in refs] == list(range(10))
    for ref in refs:
        original = blocks[by_id[ref.block_id].block_index]
        assert ref.bbox == original.bbox
        assert ref.reading_order == original.block_index
        assert ref.page_number == 1 and ref.source_method == "digital"
    caption_draft = next(d for d in plan.drafts if blocks[5].text in d.text_parts)
    assert caption_draft.text_parts == [b.text for b in blocks[5:]]
    assert plan.suppressed_low_confidence == 0
