"""OCR 인터페이스 (Sprint 2에서 정의, Sprint 2B에서 확장·구현).

구현체: app/services/ocr/tesseract.py (로컬 Tesseract 5, kor+eng).
좌표는 픽셀 원본과 PDF 시각 공간(pt) 변환 결과를 함께 보존한다.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.services.extraction.geometry import BBox


@dataclass
class OcrWord:
    bbox: BBox  # PDF 시각 공간 (pt) — 기존 하이라이트 경로와 동일
    text: str
    confidence: float  # 0.0 ~ 1.0
    # Sprint 2B 확장 (하위 호환 기본값)
    pixel_x: int = 0
    pixel_y: int = 0
    pixel_width: int = 0
    pixel_height: int = 0
    pdf_x0: float = 0.0
    pdf_y0: float = 0.0
    pdf_x1: float = 0.0
    pdf_y1: float = 0.0
    block_index: int = 0
    paragraph_index: int = 0
    line_index: int = 0
    word_index: int = 0


@dataclass
class OcrResult:
    page_number: int
    words: list[OcrWord] = field(default_factory=list)
    full_text: str = ""
    language: str = "kor+eng"
    engine: str = "none"
    engine_version: str = ""
    mean_confidence: float = 0.0
    # Sprint 2B 확장
    render_dpi: int = 0
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)


class OcrEngine(Protocol):
    """조건: kor+eng, DPI 렌더링, TSV 좌표, PDF 좌표 변환, 단어 confidence,
    timeout·취소 지원, GUI 비차단(작업 실행기에서 구동)."""

    @property
    def available(self) -> bool: ...

    @property
    def version(self) -> str: ...

    def recognize_page(
        self, pdf_path: str, page_number: int, *, language: str = "kor+eng", dpi: int = 0
    ) -> OcrResult: ...


class NoOcrEngine:
    """OCR 미탑재 상태 — 페이지는 requires_ocr 플래그로만 표시된다."""

    @property
    def available(self) -> bool:
        return False

    @property
    def version(self) -> str:
        return ""

    def recognize_page(
        self, pdf_path: str, page_number: int, *, language: str = "kor+eng", dpi: int = 0
    ) -> OcrResult:
        raise NotImplementedError("OCR engine is not bundled in this build")


def get_ocr_engine() -> OcrEngine:
    from app.services.ocr.tesseract import get_tesseract_engine

    engine = get_tesseract_engine()
    return engine if engine.available else NoOcrEngine()
