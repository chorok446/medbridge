"""OCR 인터페이스 — Sprint 2B(Tesseract 번들)에서 구현체가 채워진다.

Sprint 2 결정: Windows 실기기 검증 없이 Tesseract 실행 파일·한국어 데이터를
sidecar에 번들링하는 것은 안정성 리스크가 커서(명세 §11 fallback 조건),
이번 Sprint에서는 스캔 판정 + "OCR이 필요한 페이지입니다" 상태까지만 제공한다.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.services.extraction.geometry import BBox


@dataclass
class OcrWord:
    bbox: BBox  # PDF 페이지 좌표계 (pt) — 렌더링 DPI 좌표에서 변환해 저장
    text: str
    confidence: float  # 0.0 ~ 1.0


@dataclass
class OcrResult:
    page_number: int
    words: list[OcrWord] = field(default_factory=list)
    full_text: str = ""
    language: str = "kor+eng"
    engine: str = "none"
    engine_version: str = ""
    mean_confidence: float = 0.0


class OcrEngine(Protocol):
    """구현 조건 (Sprint 2B): kor+eng, 적정 DPI 렌더링, TSV/hOCR 좌표 사용,
    PDF 좌표 변환, 단어별 confidence, 취소 지원, GUI 비차단."""

    @property
    def available(self) -> bool: ...

    def recognize_page(self, pdf_path: str, page_number: int) -> OcrResult: ...


class NoOcrEngine:
    """OCR 미탑재 상태 — 페이지는 requires_ocr 플래그로만 표시된다."""

    @property
    def available(self) -> bool:
        return False

    def recognize_page(self, pdf_path: str, page_number: int) -> OcrResult:
        raise NotImplementedError("OCR engine is not bundled in this build")


def get_ocr_engine() -> OcrEngine:
    return NoOcrEngine()
