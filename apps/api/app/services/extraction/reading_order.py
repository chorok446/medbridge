"""읽기 순서 계산 — PDF 저장 순서가 아닌 시각적 읽기 순서를 복원한다.

지원: 단일 열, 좌우 2단, 전체 폭 제목 + 2단 본문, 하단 각주, 표·캡션.
완벽한 문단 복원을 가정하지 않으며, 불확실하면 confidence를 낮춘다.
"""

from dataclasses import dataclass

from app.services.extraction.engine import BlockRec
from app.services.extraction.thresholds import (
    COLUMN_STRADDLE_TOLERANCE,
    FOOTNOTE_ZONE_RATIO,
    FULL_WIDTH_BLOCK_RATIO,
    READING_ORDER_CONFIDENCE_FALLBACK,
    READING_ORDER_CONFIDENCE_TWO_COL,
    TWO_COLUMN_MIN_BLOCKS,
)


@dataclass
class OrderResult:
    order: list[int]  # block_index를 읽기 순서대로 나열
    confidence: float
    two_column: bool


def compute_reading_order(
    blocks: list[BlockRec], page_width: float, page_height: float
) -> OrderResult:
    text_blocks = [b for b in blocks if b.block_type == "text" and b.text.strip()]
    if not text_blocks:
        return OrderResult(
            order=[b.block_index for b in blocks], confidence=1.0, two_column=False
        )

    content_x0 = min(b.bbox[0] for b in text_blocks)
    content_width = max(max(b.bbox[2] for b in text_blocks) - content_x0, 1.0)
    mid_x = content_x0 + content_width / 2
    tolerance = page_width * COLUMN_STRADDLE_TOLERANCE
    footnote_y = page_height * FOOTNOTE_ZONE_RATIO

    KIND_SEPARATOR, KIND_LEFT, KIND_RIGHT, KIND_FOOTNOTE = 0, 1, 2, 3
    kinds: dict[int, int] = {}
    straddlers = 0
    for b in text_blocks:
        x0, y0, x1, _ = b.bbox
        if y0 >= footnote_y:
            kinds[b.block_index] = KIND_FOOTNOTE
        elif (x1 - x0) >= content_width * FULL_WIDTH_BLOCK_RATIO:
            kinds[b.block_index] = KIND_SEPARATOR
        elif x1 <= mid_x + tolerance:
            kinds[b.block_index] = KIND_LEFT
        elif x0 >= mid_x - tolerance:
            kinds[b.block_index] = KIND_RIGHT
        else:
            straddlers += 1
            kinds[b.block_index] = KIND_SEPARATOR  # 중앙 걸침은 전체 폭으로 취급

    body_count = sum(1 for k in kinds.values() if k in (KIND_LEFT, KIND_RIGHT))
    left_count = sum(1 for k in kinds.values() if k == KIND_LEFT)
    right_count = sum(1 for k in kinds.values() if k == KIND_RIGHT)
    two_column = (
        left_count >= 1
        and right_count >= 1
        and body_count >= TWO_COLUMN_MIN_BLOCKS
        and straddlers <= max(1, body_count // 3)
    )

    if not two_column:
        # 단일 열: 위→아래, 같은 높이는 왼→오. 각주는 마지막.
        ordered = sorted(
            text_blocks,
            key=lambda b: (
                kinds[b.block_index] == KIND_FOOTNOTE,  # 각주 뒤로
                round(b.bbox[1], 1),
                b.bbox[0],
            ),
        )
        confidence = 1.0 if straddlers == 0 else READING_ORDER_CONFIDENCE_FALLBACK
        return OrderResult(
            order=_with_nontext(ordered, blocks), confidence=confidence, two_column=False
        )

    # 2단: 전체 폭 블록(제목 등)이 세로 구간(band)을 나눈다.
    # band = 자기보다 완전히 위에 있는 separator 수.
    # band 안에서는 separator → 왼쪽 열(위→아래) → 오른쪽 열(위→아래), 각주는 항상 마지막.
    separator_bottoms = sorted(
        b.bbox[3] for b in text_blocks if kinds[b.block_index] == KIND_SEPARATOR
    )

    def band_of(b: BlockRec) -> int:
        top = b.bbox[1]
        if kinds[b.block_index] == KIND_SEPARATOR:
            return sum(1 for bottom in separator_bottoms if bottom <= b.bbox[1] + 0.001)
        return sum(1 for bottom in separator_bottoms if bottom <= top + 0.001)

    ordered = sorted(
        text_blocks,
        key=lambda b: (
            kinds[b.block_index] == KIND_FOOTNOTE,  # 각주는 페이지 마지막
            band_of(b),
            # band 안에서 왼쪽(0) → 오른쪽(1) → separator(2):
            # separator는 자기 band를 닫는 블록이므로 그 band 본문 뒤에 온다
            {KIND_LEFT: 0, KIND_RIGHT: 1, KIND_SEPARATOR: 2}.get(kinds[b.block_index], 0),
            b.bbox[1],
            b.bbox[0],
        ),
    )
    confidence = (
        READING_ORDER_CONFIDENCE_TWO_COL if straddlers == 0 else READING_ORDER_CONFIDENCE_FALLBACK
    )
    return OrderResult(order=_with_nontext(ordered, blocks), confidence=confidence, two_column=True)


def _with_nontext(ordered_text: list[BlockRec], all_blocks: list[BlockRec]) -> list[int]:
    """텍스트 블록 순서 뒤에 비텍스트(이미지 등) 블록을 원본 순서로 덧붙인다."""
    order = [b.block_index for b in ordered_text]
    seen = set(order)
    order.extend(b.block_index for b in all_blocks if b.block_index not in seen)
    return order
