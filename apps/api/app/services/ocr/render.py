"""OCR용 페이지 이미지 렌더링 — 원본은 수정하지 않고 임시 파일에만 그린다."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.services.ocr.settings import OCR_MAX_IMAGE_PIXELS


@dataclass
class RenderedPage:
    path: Path  # 임시 PNG (호출자가 삭제)
    pixel_width: int
    pixel_height: int
    effective_dpi: int
    page_width_pt: float  # 시각(회전 적용) 공간
    page_height_pt: float
    warnings: list[str]


def render_page(pdf_path: str, page_number: int, dpi: int) -> RenderedPage:
    """페이지를 grayscale PNG로 렌더링. 회전은 pixmap에 반영되므로
    픽셀 공간 == 시각 공간 × (dpi/72)."""
    warnings: list[str] = []
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_number - 1]
        rect = page.rect
        # 메모리 보호: 최대 픽셀 수 초과 시 DPI 하향
        effective_dpi = dpi
        while effective_dpi > 72:
            px_w = rect.width * effective_dpi / 72
            px_h = rect.height * effective_dpi / 72
            if px_w * px_h <= OCR_MAX_IMAGE_PIXELS:
                break
            effective_dpi -= 50
        if effective_dpi != dpi:
            warnings.append(f"dpi_reduced:{dpi}->{effective_dpi}")

        # 원본 내장 이미지 해상도가 렌더 DPI보다 크게 낮으면 품질 경고
        try:
            infos = page.get_image_info()
            if infos:
                best = max(min(i.get("xres", 0), i.get("yres", 0)) for i in infos)
                if 0 < best < effective_dpi * 0.6:
                    warnings.append(f"low_source_resolution:{best}dpi")
        except Exception:
            pass

        pix = page.get_pixmap(dpi=effective_dpi, colorspace=pymupdf.csGRAY, alpha=False)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        pix.save(tmp.name)
        return RenderedPage(
            path=Path(tmp.name),
            pixel_width=pix.width,
            pixel_height=pix.height,
            effective_dpi=effective_dpi,
            page_width_pt=float(rect.width),
            page_height_pt=float(rect.height),
            warnings=warnings,
        )
    finally:
        doc.close()
