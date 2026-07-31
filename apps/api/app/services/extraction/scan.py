"""스캔 페이지 판정 — 페이지 단위로 digital/mixed/scanned/unknown을 결정한다.

임계값은 thresholds.py에 있다. 텍스트가 있는 PDF라도 일부 페이지만 스캔일 수 있다.
"""

from dataclasses import dataclass

from app.models.enums import ScanVerdict
from app.services.extraction.normalize import valid_char_ratio
from app.services.extraction.thresholds import (
    OCR_REQUIRED_MIXED_MAX_CHARS,
    SCAN_DIGITAL_MIN_CHARS,
    SCAN_FULL_PAGE_IMAGE_RATIO,
    SCAN_IMAGE_RATIO_MIXED,
    SCAN_IMAGE_RATIO_SCANNED,
    SCAN_MIN_CHARS,
    SCAN_VALID_CHAR_RATIO,
)


@dataclass
class ScanResult:
    verdict: ScanVerdict
    requires_ocr: bool
    confidence: float


def classify_page(
    *,
    char_count: int,
    word_count: int,
    image_area_ratio: float,
    full_page_image: bool,
    has_text_blocks: bool,
    raw_text: str,
) -> ScanResult:
    valid_ratio = valid_char_ratio(raw_text)

    # 텍스트가 사실상 없는 페이지
    if char_count < SCAN_MIN_CHARS or not has_text_blocks:
        if full_page_image or image_area_ratio >= SCAN_IMAGE_RATIO_SCANNED:
            return ScanResult(ScanVerdict.SCANNED, requires_ocr=True, confidence=0.9)
        if image_area_ratio > 0.05:
            return ScanResult(ScanVerdict.SCANNED, requires_ocr=True, confidence=0.6)
        # 이미지도 텍스트도 없는 빈 페이지
        return ScanResult(ScanVerdict.UNKNOWN, requires_ocr=False, confidence=0.5)

    # 추출은 됐지만 문자 매핑이 깨진 경우 (글꼴 문제 등)
    if valid_ratio < SCAN_VALID_CHAR_RATIO:
        return ScanResult(ScanVerdict.UNKNOWN, requires_ocr=True, confidence=0.5)

    # 텍스트 + 큰 이미지 공존
    if full_page_image and char_count < SCAN_DIGITAL_MIN_CHARS:
        return ScanResult(ScanVerdict.SCANNED, requires_ocr=True, confidence=0.7)
    if image_area_ratio >= SCAN_IMAGE_RATIO_MIXED:
        requires_ocr = char_count <= OCR_REQUIRED_MIXED_MAX_CHARS
        return ScanResult(ScanVerdict.MIXED, requires_ocr=requires_ocr, confidence=0.8)

    return ScanResult(ScanVerdict.DIGITAL, requires_ocr=False, confidence=0.95)


_ = SCAN_FULL_PAGE_IMAGE_RATIO  # 엔진 쪽 full_page_image 판정과 같은 문서에 명시된 임계값
