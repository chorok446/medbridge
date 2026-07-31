"""머리말·꼬리말 탐지 — 여러 페이지에서 반복되는 상·하단 블록을 표시한다.

원문은 삭제하지 않고 is_header/is_footer 플래그만 남긴다 (사용자가 포함해 볼 수 있음).
신호: 반복 빈도 · 페이지 내 상대 위치 · 텍스트 유사도(숫자 무시) · 글자 길이.
"""

import re
from collections import Counter

from app.services.extraction.engine import PageData
from app.services.extraction.normalize import is_page_number_line
from app.services.extraction.thresholds import (
    FOOTER_ZONE_RATIO,
    HEADER_MAX_CHARS,
    HEADER_ZONE_RATIO,
    REPEAT_MIN_FRACTION,
    REPEAT_MIN_PAGES,
)

_DIGITS = re.compile(r"\d+")


def _fingerprint(text: str) -> str:
    """페이지 번호 등 숫자 변동을 무시한 반복 비교 키."""
    return _DIGITS.sub("#", " ".join(text.split()).lower())[:HEADER_MAX_CHARS]


def _zone(block_bbox: tuple[float, float, float, float], page_height: float) -> str | None:
    if block_bbox[3] <= page_height * HEADER_ZONE_RATIO * 1.6 and (
        block_bbox[1] <= page_height * HEADER_ZONE_RATIO
    ):
        return "header"
    if block_bbox[1] >= page_height * FOOTER_ZONE_RATIO:
        return "footer"
    return None


def detect_repeated_bands(pages: list[PageData]) -> dict[int, dict[int, str]]:
    """반환: {page_number: {block_index: "header"|"footer"}}"""
    total_pages = len(pages)
    if total_pages == 0:
        return {}
    threshold = max(REPEAT_MIN_PAGES, int(total_pages * REPEAT_MIN_FRACTION))

    counts: Counter[tuple[str, str]] = Counter()
    for page in pages:
        seen_on_page: set[tuple[str, str]] = set()
        for block in page.blocks:
            if block.block_type != "text" or not block.text.strip():
                continue
            if len(block.text) > HEADER_MAX_CHARS:
                continue
            zone = _zone(block.bbox, page.height)
            if zone is None:
                continue
            key = (zone, _fingerprint(block.text))
            if key not in seen_on_page:  # 같은 페이지 중복 카운트 방지
                counts[key] += 1
                seen_on_page.add(key)

    repeated = {key for key, n in counts.items() if n >= threshold}

    result: dict[int, dict[int, str]] = {}
    for page in pages:
        marks: dict[int, str] = {}
        for block in page.blocks:
            if block.block_type != "text" or not block.text.strip():
                continue
            zone = _zone(block.bbox, page.height)
            if zone is None:
                continue
            text = block.text.strip()
            # 반복 블록이거나, 단독 페이지 번호 행이면 표시
            if (zone, _fingerprint(text)) in repeated or (
                len(text) <= 12 and is_page_number_line(text)
            ):
                marks[block.block_index] = zone
        if marks:
            result[page.page_number] = marks
    return result
