"""추출 단위 테스트 — 좌표·정규화·읽기 순서·스캔 판정·머리말 탐지."""

import pymupdf
import pytest

from app.models.enums import ScanVerdict
from app.services.extraction import engine as engine_mod
from app.services.extraction.geometry import clamp_bbox, overlap_ratio
from app.services.extraction.headers import detect_repeated_bands
from app.services.extraction.normalize import (
    is_page_number_line,
    normalize_text,
    valid_char_ratio,
)
from app.services.extraction.reading_order import compute_reading_order
from app.services.extraction.scan import classify_page
from tests import extraction_fixtures as fx


def _pages(data: bytes) -> list:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [engine_mod.extract_page(doc, i) for i in range(doc.page_count)]
    finally:
        doc.close()


class TestGeometry:
    def test_clamp_orders_and_bounds(self):
        assert clamp_bbox((-5, 900, 700, -3), 595, 842) == (0.0, 0.0, 595.0, 842.0)

    def test_clamp_swaps_inverted(self):
        x0, y0, x1, y1 = clamp_bbox((100, 200, 50, 120), 595, 842)
        assert x0 <= x1 and y0 <= y1

    def test_rounding_policy(self):
        bbox = clamp_bbox((1.23456, 2.34567, 3.45678, 4.56789), 595, 842)
        assert bbox == (1.23, 2.35, 3.46, 4.57)

    def test_overlap_ratio(self):
        assert overlap_ratio((0, 0, 10, 10), (0, 0, 5, 10)) == pytest.approx(0.5)


class TestNormalize:
    def test_hyphen_join(self):
        assert "cardio" not in normalize_text("myocar-\ndial infarction")
        assert "myocardial" in normalize_text("myocar-\ndial infarction")

    def test_preserves_numbers_units_symbols(self):
        raw = "EF 45.5% (±2.3), BP ≥140 mmHg"
        out = normalize_text(raw)
        for token in ["45.5%", "±2.3", "≥140", "mmHg", "("]:
            assert token in out

    def test_control_chars_removed(self):
        assert normalize_text("a\x00b\x1fc") == "abc"

    def test_page_number_line(self):
        assert is_page_number_line(" 12 ")
        assert is_page_number_line("- 3 -")
        assert not is_page_number_line("12명의 환자")

    def test_valid_char_ratio(self):
        assert valid_char_ratio("정상 텍스트 abc") == 1.0


class TestScanClassify:
    def test_digital(self):
        r = classify_page(
            char_count=500, word_count=100, image_area_ratio=0.05,
            full_page_image=False, has_text_blocks=True, raw_text="정상 본문" * 100,
        )
        assert r.verdict == ScanVerdict.DIGITAL and not r.requires_ocr

    def test_scanned_full_image_no_text(self):
        r = classify_page(
            char_count=0, word_count=0, image_area_ratio=0.98,
            full_page_image=True, has_text_blocks=False, raw_text="",
        )
        assert r.verdict == ScanVerdict.SCANNED and r.requires_ocr

    def test_mixed_text_over_image(self):
        r = classify_page(
            char_count=300, word_count=60, image_area_ratio=0.6,
            full_page_image=False, has_text_blocks=True, raw_text="본문" * 200,
        )
        assert r.verdict == ScanVerdict.MIXED and not r.requires_ocr

    def test_blank_page_is_unknown_without_ocr(self):
        r = classify_page(
            char_count=0, word_count=0, image_area_ratio=0.0,
            full_page_image=False, has_text_blocks=False, raw_text="",
        )
        assert r.verdict == ScanVerdict.UNKNOWN and not r.requires_ocr

    def test_broken_font_mapping_needs_ocr(self):
        r = classify_page(
            char_count=300, word_count=50, image_area_ratio=0.0,
            full_page_image=False, has_text_blocks=True, raw_text="�" * 300,
        )
        assert r.requires_ocr


class TestEngineOnFixtures:
    def test_page_dims_and_rotation(self):
        pages = _pages(fx.rotated_page())
        assert pages[0].rotation == 90
        # 회전 적용 후 rect 기준이므로 가로가 더 길다
        assert pages[0].width > pages[0].height

    def test_blocks_words_within_page(self):
        for page in _pages(fx.two_column_english(pages=1)):
            for b in page.blocks:
                assert 0 <= b.bbox[0] <= b.bbox[2] <= page.width
                assert 0 <= b.bbox[1] <= b.bbox[3] <= page.height
            assert page.words, "단어 추출 실패"

    def test_table_extracted(self):
        pages = _pages(fx.with_table())
        tables = pages[0].tables
        assert tables and tables[0].row_count >= 3 and tables[0].column_count == 3
        assert "심박수" in str(tables[0].cells)


class TestReadingOrder:
    def test_single_column_top_to_bottom(self):
        pages = _pages(fx.single_column_korean(pages=1))
        page = pages[0]
        result = compute_reading_order(page.blocks, page.width, page.height)
        assert not result.two_column
        texts = _ordered_texts(page, result.order)
        assert texts.index("심부전의 이해 - 1장") < texts.index(
            next(t for t in texts if t.startswith("문단 1."))
        )

    def test_two_column_left_before_right(self):
        pages = _pages(fx.two_column_english(pages=1))
        page = pages[0]
        result = compute_reading_order(page.blocks, page.width, page.height)
        assert result.two_column
        joined = "\n".join(_ordered_texts(page, result.order))
        # 같은 band에서 왼쪽 열 전체가 오른쪽 열보다 먼저
        assert joined.index("L0-0") < joined.index("R0-0")
        assert joined.index("L0-13") < joined.index("R0-0")
        # 전체 폭 제목이 본문보다 먼저
        assert joined.index("Effects of Exercise") < joined.index("L0-0")


def _ordered_texts(page, order):
    by_index = {b.block_index: b for b in page.blocks}
    return [by_index[i].text for i in order if by_index[i].text.strip()]


class TestHeaderFooter:
    def test_repeated_bands_detected(self):
        pages = _pages(fx.two_column_english(pages=3))
        marks = detect_repeated_bands(pages)
        # 머리말(저널명)과 꼬리말(페이지 번호)이 각 페이지에서 표시된다
        assert marks, "반복 밴드 미탐지"
        zones = {z for page_marks in marks.values() for z in page_marks.values()}
        assert "header" in zones
        assert "footer" in zones
