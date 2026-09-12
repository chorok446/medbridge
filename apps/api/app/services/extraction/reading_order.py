"""읽기 순서 계산 — PDF 저장 순서가 아닌 시각적 읽기 순서를 복원한다.

지원: 단일 열, 좌우 2단, 전체 폭 제목 + 2단 본문, 하단 각주, 표·캡션.
완벽한 문단 복원을 가정하지 않으며, 불확실하면 confidence를 낮춘다.
"""

import re
from bisect import bisect_right
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

_NUMBERED_TABLE_CAPTION = re.compile(
    r"^(?:표\s*|table\s+)\d+(?:[.-]\d+)*[.,:]?\s+\S", re.IGNORECASE
)
_SPLIT_NUMBERED_SECTION = re.compile(r"[0-9]+\.[ \t]*\r?\n[^\r\n]+")
_CAPTION_MAX_CHARS = 96
_SECTION_MAX_CHARS = 64
_CAPTION_ROW_GAP = 40.0  # 화면 좌표계의 PDF point 단위


@dataclass
class OrderResult:
    order: list[int]  # block_index를 읽기 순서대로 나열
    confidence: float
    two_column: bool
    section_heading_indices: frozenset[int] = frozenset()


def compute_reading_order(
    blocks: list[BlockRec], page_width: float, page_height: float
) -> OrderResult:
    text_blocks = [b for b in blocks if b.block_type == "text" and b.text.strip()]
    if not text_blocks:
        return OrderResult(order=[b.block_index for b in blocks], confidence=1.0, two_column=False)

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
    boundaries, sections = _heading_boundaries(
        text_blocks, content_x0, content_width, tolerance, footnote_y
    )
    if boundaries:
        # 검증된 제목의 위쪽을 경계로 안정 정렬한다. 기존 구간 내부의
        # 좌→우 열 순서는 보존하며, 선행 본문을 제목 위 구간으로 되돌린다.
        regrouped = sorted(
            ordered,
            key=lambda b: (b.bbox[1] >= footnote_y, bisect_right(boundaries, b.bbox[1])),
        )
        if regrouped != ordered:
            confidence = min(confidence, READING_ORDER_CONFIDENCE_FALLBACK)
            ordered = regrouped
    return OrderResult(
        order=_with_nontext(ordered, blocks), confidence=confidence, two_column=True,
        section_heading_indices=sections,
    )


def _heading_boundaries(
    blocks: list[BlockRec], content_x0: float, content_width: float,
    tolerance: float, footnote_y: float,
) -> tuple[list[float], frozenset[int]]:
    """좌측 번호 제목 + 인접 전폭 본문으로 확인되는 경계만 사용한다.

    표 구조나 행·열 관계를 추론하지 않는다. 독립 열 또는 경계를 가로지르는
    본문이 있으면 기존 순서를 유지한다. 이미지 bbox는 텍스트 흐름을 나누지 않는다.
    """
    body = [b for b in blocks if b.bbox[1] < footnote_y]
    full_width = content_width * FULL_WIDTH_BLOCK_RATIO
    boundaries = []
    sections: set[int] = set()
    for caption in body:
        x0, top, x1, bottom = caption.bbox
        label = caption.text.strip()
        if (
            x0 > content_x0 + tolerance or x1 - x0 >= full_width
            or len(label) > _CAPTION_MAX_CHARS or len(label.splitlines()) > 2
        ):
            continue
        is_caption = bool(_NUMBERED_TABLE_CAPTION.match(label))
        is_section = not is_caption and _is_standalone_section(
            caption, body, full_width
        )
        if not is_caption and not is_section:
            continue
        if not any(
            b.bbox[2] - b.bbox[0] >= full_width
            and bottom <= b.bbox[1] <= bottom + _CAPTION_ROW_GAP
            for b in body
        ):
            continue
        if any(
            b is not caption and (
                b.bbox[1] < top < b.bbox[3]
                or (top <= b.bbox[1] < bottom and b.bbox[0] >= x1)
            )
            for b in body
        ):
            continue
        boundaries.append(top)
        if is_section:
            sections.add(caption.block_index)
    return sorted(set(boundaries)), frozenset(sections)


def _is_standalone_section(heading: BlockRec, body: list[BlockRec], full_width: float) -> bool:
    """번호만 첫 줄에 있는 짧은 절 제목에 추가적인 공간 근거를 요구한다."""
    label = heading.text.strip()
    if (
        len(label) > _SECTION_MAX_CHARS or not _SPLIT_NUMBERED_SECTION.fullmatch(label)
        or label.endswith((".", "?", "!", ",", ";", ":", "。", "？", "！"))
    ):
        return False
    top, bottom = heading.bbox[1], heading.bbox[3]
    if any(b is not heading and top <= b.bbox[1] < bottom for b in body):
        return False  # 캡션과 달리 절 제목에는 겹쳐 시작하는 본문을 허용하지 않는다.
    above = [b.bbox[3] for b in body if b.bbox[1] < top]
    if bottom <= top or not above or top - max(above) < bottom - top:
        return False  # 제목 높이 이상의 여백이 없으면 목록/연속 본문으로 남긴다.
    below = [b for b in body if b.bbox[1] >= bottom and b is not heading]
    if not below:
        return False
    nearest_top = min(b.bbox[1] for b in below)
    return all(
        b.bbox[2] - b.bbox[0] >= full_width
        for b in below if b.bbox[1] == nearest_top
    )


def _with_nontext(ordered_text: list[BlockRec], all_blocks: list[BlockRec]) -> list[int]:
    """텍스트 블록 순서 뒤에 비텍스트(이미지 등) 블록을 원본 순서로 덧붙인다."""
    order = [b.block_index for b in ordered_text]
    seen = set(order)
    order.extend(b.block_index for b in all_blocks if b.block_index not in seen)
    return order
