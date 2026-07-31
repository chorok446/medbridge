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
    SCAN_MIN_TEXT_AREA_RATIO,
    SCAN_MIN_WORDS,
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
    has_images: bool,
    text_area_ratio: float,
    raw_text: str,
) -> ScanResult:
    valid_ratio = valid_char_ratio(raw_text)

    # 텍스트가 사실상 없는 페이지 — 글자 수만이 아니라 단어 수·블록 존재까지 함께 본다.
    # (실제 스캐너 인코딩에서 raw_text가 소량의 깨진 문자만 뽑아내는 경우가 있다)
    if char_count < SCAN_MIN_CHARS or word_count < SCAN_MIN_WORDS or not has_text_blocks:
        if has_images:
            # image_area_ratio/full_page_image는 CCITT·JBIG2 등 일부 스캐너 인코딩에서
            # 신뢰할 수 없게 작게 나올 수 있다. 텍스트가 없고 이미지가 하나라도 있으면
            # (면적 계산 성패와 무관하게) OCR 대상으로 판정한다 — 면적 값은 확신도에만 반영.
            confident = full_page_image or image_area_ratio >= SCAN_IMAGE_RATIO_SCANNED
            confidence = 0.9 if confident else 0.6
            return ScanResult(ScanVerdict.SCANNED, requires_ocr=True, confidence=confidence)
        # 이미지도 텍스트도 없는 빈 페이지
        return ScanResult(ScanVerdict.UNKNOWN, requires_ocr=False, confidence=0.5)

    # 추출은 됐지만 문자 매핑이 깨진 경우 (글꼴 문제 등)
    if valid_ratio < SCAN_VALID_CHAR_RATIO:
        return ScanResult(ScanVerdict.UNKNOWN, requires_ocr=True, confidence=0.5)

    # 텍스트 + 큰 이미지 공존 — full_page_image가 실패해도 이미지가 있고 텍스트가
    # 페이지의 극히 일부만 차지한다면(text_area_ratio) 나머지는 이미지라는 신호로
    # 함께 쓴다. has_images 없이는 쓰지 않는다 — 그렇지 않으면 큰 글자 한 줄짜리
    # 제목·구분 페이지처럼 이미지가 전혀 없는 정상 디지털 페이지까지
    # SCANNED로 잘못 승격된다 (Codex 리뷰로 발견).
    low_text_area = text_area_ratio < SCAN_MIN_TEXT_AREA_RATIO
    sparse_text_area = has_images and (full_page_image or low_text_area)
    if sparse_text_area and char_count < SCAN_DIGITAL_MIN_CHARS:
        return ScanResult(ScanVerdict.SCANNED, requires_ocr=True, confidence=0.7)
    if image_area_ratio >= SCAN_IMAGE_RATIO_MIXED:
        requires_ocr = char_count <= OCR_REQUIRED_MIXED_MAX_CHARS
        return ScanResult(ScanVerdict.MIXED, requires_ocr=requires_ocr, confidence=0.8)

    return ScanResult(ScanVerdict.DIGITAL, requires_ocr=False, confidence=0.95)


_ = SCAN_FULL_PAGE_IMAGE_RATIO  # 엔진 쪽 full_page_image 판정과 같은 문서에 명시된 임계값
