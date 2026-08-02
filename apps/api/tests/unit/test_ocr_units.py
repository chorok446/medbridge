"""OCR 단위 테스트 — TSV 파싱·좌표 변환·품질 분류·렌더링 보호장치."""

import os

import pytest

from app.models.enums import OcrRunStatus
from app.services.extraction.ocr import OcrResult, OcrWord
from app.services.ocr.service import classify_result
from app.services.ocr.tesseract import TsvWord, parse_tsv, pixels_to_pdf

TSV_SAMPLE = "\t".join(
    [
        "level", "page_num", "block_num", "par_num", "line_num", "word_num",
        "left", "top", "width", "height", "conf", "text",
    ]
) + "\n" + "\n".join(
    [
        "1\t1\t0\t0\t0\t0\t0\t0\t2480\t3508\t-1\t",
        "5\t1\t1\t1\t1\t1\t100\t200\t300\t80\t96.5\t심장은",
        "5\t1\t1\t1\t1\t2\t420\t200\t250\t80\t88.0\t혈액을",
        "5\t1\t1\t1\t2\t1\t100\t300\t200\t80\t45.0\tEF",
        "5\t1\t1\t1\t2\t2\t320\t300\t150\t80\t-1\t",  # conf<0 제외
        "5\t1\t1\t1\t2\t3\t500\t300\t100\t80\t70.0\t  ",  # 공백 제외
    ]
)


class TestTsvParse:
    def test_words_with_hierarchy_and_confidence(self):
        words = parse_tsv(TSV_SAMPLE)
        assert len(words) == 3
        first = words[0]
        assert first.text == "심장은"
        assert first.confidence == pytest.approx(0.965)
        assert (first.block_index, first.paragraph_index, first.line_index, first.word_index) == (
            1, 1, 1, 1,
        )

    def test_malformed_rows_skipped(self):
        assert parse_tsv("garbage\nnot\ttsv") == []


class TestPixelToPdf:
    def test_scale_by_dpi(self):
        w = TsvWord("x", 0.9, left=300, top=600, width=300, height=150,
                    block_index=1, paragraph_index=1, line_index=1, word_index=1)
        # 300dpi → pt = px * 72/300
        box = pixels_to_pdf(w, 300, page_width_pt=595, page_height_pt=842)
        assert box == (72.0, 144.0, 144.0, 180.0)

    def test_clamped_to_page(self):
        w = TsvWord("x", 0.9, left=2400, top=3400, width=500, height=500,
                    block_index=1, paragraph_index=1, line_index=1, word_index=1)
        box = pixels_to_pdf(w, 300, page_width_pt=595, page_height_pt=842)
        assert box[2] <= 595 and box[3] <= 842


def _result(confidences: list[float]) -> OcrResult:
    words = [
        OcrWord(bbox=(0, 0, 1, 1), text=f"w{i}", confidence=c)
        for i, c in enumerate(confidences)
    ]
    return OcrResult(page_number=1, words=words)


class TestClassify:
    def test_completed(self):
        status, low, median = classify_result(_result([0.9] * 10))
        assert status == OcrRunStatus.OCR_COMPLETED and low == 0.0

    def test_low_confidence(self):
        status, low, _ = classify_result(_result([0.9] * 5 + [0.3] * 5))
        assert status == OcrRunStatus.OCR_LOW_CONFIDENCE
        assert low == pytest.approx(0.5)

    def test_empty(self):
        status, _, _ = classify_result(_result([0.9]))
        assert status == OcrRunStatus.OCR_EMPTY


class TestRenderGuards:
    def test_dpi_reduced_for_huge_page(self, tmp_path):
        import pymupdf

        from app.services.ocr.render import render_page

        doc = pymupdf.open()
        doc.new_page(width=2000, height=2000)  # 300dpi면 ~69MP → 자동 하향
        path = tmp_path / "huge.pdf"
        doc.save(str(path))
        rendered = render_page(str(path), 1, 300)
        try:
            assert rendered.effective_dpi < 300
            assert any(w.startswith("dpi_reduced") for w in rendered.warnings)
            assert rendered.pixel_width * rendered.pixel_height <= 40_000_000
        finally:
            rendered.path.unlink(missing_ok=True)

    def test_temp_file_created_and_removable(self, tmp_path):
        import pymupdf

        from app.services.ocr.render import render_page

        doc = pymupdf.open()
        doc.new_page()
        path = tmp_path / "p.pdf"
        doc.save(str(path))
        rendered = render_page(str(path), 1, 150)
        assert rendered.path.is_file()
        rendered.path.unlink()
        assert not rendered.path.exists()


class TestExtendedLengthPrefix:
    r"""Tauri resource_dir()는 Windows에서 `\\?\C:\...`를 돌려준다.

    Python은 이 형태를 문제없이 다뤄 바이너리 탐지도 `--version`도 성공하지만,
    tesseract는 TESSDATA_PREFIX에 "/eng.traineddata"를 이어 붙이고 Win32는 `\\?\`
    경로를 정규화하지 않아 파일을 못 연다. 실기기에서 45페이지가 전량 이 이유로 실패했다.
    """

    def test_drive_prefix_is_stripped(self):
        from app.services.ocr.tesseract import strip_extended_prefix

        assert (
            strip_extended_prefix(r"\\?\C:\Users\u\MedBridge Study\resources\ocr")
            == r"C:\Users\u\MedBridge Study\resources\ocr"
        )

    def test_unc_prefix_becomes_a_plain_unc_path(self):
        from app.services.ocr.tesseract import strip_extended_prefix

        assert strip_extended_prefix(r"\\?\UNC\server\share\ocr") == r"\\server\share\ocr"

    def test_plain_paths_and_none_are_untouched(self):
        from app.services.ocr.tesseract import strip_extended_prefix

        assert strip_extended_prefix(r"C:\ocr") == r"C:\ocr"
        assert strip_extended_prefix("/usr/share/ocr") == "/usr/share/ocr"
        assert strip_extended_prefix(None) is None
        assert strip_extended_prefix("") == ""

    def test_engine_resolves_binary_through_a_prefixed_dir(self, tmp_path, monkeypatch):
        """접두사가 붙은 채로 오면 벗겨서 쓴다 — tessdata 경로가 tesseract에 그대로 간다."""
        from app.services.ocr import tesseract as mod

        ocr = tmp_path / "ocr"
        (ocr / "tessdata").mkdir(parents=True)
        binary = ocr / ("tesseract.exe" if os.name == "nt" else "tesseract")
        binary.write_text("", encoding="utf-8")

        monkeypatch.setenv(
            "MEDBRIDGE_OCR_DIR", rf"\\?\{ocr}" if os.name == "nt" else str(ocr)
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeVersion())

        engine = mod.TesseractEngine()

        assert engine.available
        assert "\\?\\" not in (engine._tessdata or "")
        assert engine._tessdata == str(ocr / "tessdata")


class _FakeVersion:
    stdout = "tesseract v5.4.0\n"
    stderr = ""
