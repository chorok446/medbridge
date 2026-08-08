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

    def test_dropped_noise_counts_against_the_page(self):
        """임계값 미만이라 버린 단어도 판정 모수에 든다.

        OCR_MIN_WORD_CONFIDENCE 필터가 OcrResult를 만들기 전에 걸리므로, 버려진
        몫을 세지 않으면 100단어 중 85개가 노이즈인 쪽이 '남은 15개가 깨끗하니
        정상 완료'로 통과한다. 그러면 rollup은 이 쪽을 unresolved로 세지 않아
        문서를 EXTRACTED로 승격시키고, chunking의 저신뢰 제외에도 걸리지 않아
        15단어짜리 잔해가 요약·검색의 근거가 된다. 사용자는 본문 85%가 빠진
        문서를 '다 읽었다'로 신뢰한다.
        """
        result = _result([0.75] * 15)
        result.low_quality_dropped = 85

        status, low, _ = classify_result(result)

        assert status == OcrRunStatus.OCR_LOW_CONFIDENCE
        # 버린 85개는 임계값 미만이 확실하므로 저신뢰로 센다.
        assert low == pytest.approx(0.85)

    def test_a_few_dropped_words_do_not_condemn_a_clean_page(self):
        # 깨끗한 쪽에도 얼룩 몇 개는 늘 섞인다 — 그것만으로 저신뢰가 되면
        # 정상 문서마다 'OCR 실패'가 뜬다.
        result = _result([0.9] * 95)
        result.low_quality_dropped = 5

        status, low, _ = classify_result(result)

        assert status == OcrRunStatus.OCR_COMPLETED
        assert low == pytest.approx(0.05)

    def test_all_words_dropped_is_empty_not_completed(self):
        # 전부 노이즈라 하나도 안 남았으면 '읽을 글자가 없었다'와 같다.
        result = _result([])
        result.low_quality_dropped = 40

        status, _, _ = classify_result(result)

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


class TestDescribeLeaksNoPath:
    """엔진 스냅샷이 계정 이름을 오류 보고서로 실어 나르지 않게 한다.

    NSIS 설치 모드가 currentUser라 resource_dir()는 `C:\\Users\\<계정>\\...`가 되고,
    그 값이 MEDBRIDGE_OCR_DIR → describe()의 경로 필드로 흘러간다. ocr_job이 잡마다
    이걸 로그에 쓰고, error-report가 sidecar.log 꼬리 200줄을 zip에 담는다. 한국에서
    Windows 계정 이름은 대개 실명이다 — logging.py가 "개인정보는 남기지 않는다"고
    못박은 계약을 정면으로 깬다.
    """

    def _engine(self, tmp_path, monkeypatch, user: str):
        from app.services.ocr import tesseract as mod

        ocr = tmp_path / "Users" / user / "AppData" / "MedBridge" / "resources" / "ocr"
        (ocr / "tessdata" / "configs").mkdir(parents=True)
        (ocr / "tessdata" / "kor.traineddata").write_text("", encoding="utf-8")
        (ocr / "tessdata" / "eng.traineddata").write_text("", encoding="utf-8")
        (ocr / "tessdata" / "configs" / "tsv").write_text("", encoding="utf-8")
        (ocr / ("tesseract.exe" if os.name == "nt" else "tesseract")).write_text(
            "", encoding="utf-8"
        )
        monkeypatch.setenv("MEDBRIDGE_OCR_DIR", str(ocr))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeVersion())
        return mod.TesseractEngine()

    def test_no_field_carries_the_account_name(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path, monkeypatch, "김민준")

        snapshot = engine.describe()

        flat = " ".join(f"{k}={v}" for k, v in snapshot.items())
        assert "김민준" not in flat, snapshot
        # 경로 조각 자체가 없어야 한다 — 계정 이름은 그 안에만 들어 있다.
        assert "Users" not in flat and "AppData" not in flat, snapshot

    def test_still_answers_what_is_missing(self, tmp_path, monkeypatch):
        """경로를 뺐다고 진단력을 잃으면 안 된다 — 이 스냅샷의 존재 이유다."""
        engine = self._engine(tmp_path, monkeypatch, "김민준")

        snapshot = engine.describe()

        assert snapshot["binary_found"] is True
        assert snapshot["tessdata_found"] is True
        assert snapshot["tessdata_langs"] == "eng,kor"
        assert snapshot["tsv_config"] is True
        assert snapshot["ocr_dir_env"] is True
        assert snapshot["version"] == "v5.4.0"

    def test_reports_a_missing_tessdata(self, tmp_path, monkeypatch):
        from app.services.ocr import tesseract as mod

        ocr = tmp_path / "ocr"
        ocr.mkdir()
        (ocr / ("tesseract.exe" if os.name == "nt" else "tesseract")).write_text(
            "", encoding="utf-8"
        )
        monkeypatch.setenv("MEDBRIDGE_OCR_DIR", str(ocr))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeVersion())

        snapshot = mod.TesseractEngine().describe()

        assert snapshot["binary_found"] is True
        assert snapshot["tessdata_found"] is False
        assert snapshot["tessdata_langs"] == ""


def _tsv(good: int, noise: int) -> str:
    """잘 읽힌 단어 `good`개(conf 0.75)와 노이즈 `noise`개(conf 0.08)."""
    rows = [
        "\t".join(
            ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
             "left", "top", "width", "height", "conf", "text"]
        )
    ]
    for i in range(good):
        rows.append(f"5\t1\t1\t1\t1\t{i + 1}\t{i * 10}\t100\t50\t20\t75.0\t단어{i}")
    for i in range(noise):
        rows.append(f"5\t1\t1\t1\t2\t{i + 1}\t{i * 10}\t200\t50\t20\t8.0\t노이즈{i}")
    return "\n".join(rows)


def _stub_engine(monkeypatch, tmp_path, tsv: str):
    """렌더·subprocess를 대신해 주어진 TSV를 돌려주는 엔진."""
    from app.services.ocr import tesseract as mod

    engine = mod.TesseractEngine.__new__(mod.TesseractEngine)
    engine._binary = "tesseract"
    engine._tessdata = None
    engine._version = "5.4.0"

    class _Rendered:
        path = tmp_path / "p.png"
        effective_dpi = 300
        page_width_pt = 595.0
        page_height_pt = 842.0
        warnings: list[str] = []

    _Rendered.path.write_bytes(b"")
    monkeypatch.setattr(mod, "render_page", lambda *a, **k: _Rendered())
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: type(
            "P", (), {"returncode": 0, "stdout": tsv.encode(), "stderr": b""}
        )(),
    )
    return engine


class TestNoiseDropIsVisible:
    """0.20 미만으로 버린 단어가 어느 지표에도 남지 않던 문제.

    `OCR_MIN_WORD_CONFIDENCE` 필터가 `OcrResult`를 만들기 **전에** 걸리므로,
    mean_confidence도 classify_result의 low_ratio도 raw_words도 전부 살아남은
    단어만 센다. 한 쪽의 85%가 노이즈로 버려져도 로그에는 "신뢰도 0.75, 정상 완료"로
    남아, "요약에 이 내용이 왜 없나"를 물을 때 좁힐 단서가 하나도 없다.
    """

    def test_dropped_word_count_survives_the_noise_filter(self, tmp_path, monkeypatch):
        engine = _stub_engine(monkeypatch, tmp_path, _tsv(good=15, noise=85))

        result = engine.recognize_page("x.pdf", 1)

        # 살아남은 단어만 보면 이 쪽은 깨끗해 보인다 — 그게 문제였다.
        assert len(result.words) == 15
        assert result.mean_confidence == pytest.approx(0.75)
        # 버려진 85단어가 숫자로 남아야 "깨끗함"과 "대부분 버림"이 로그에서 갈린다.
        assert result.low_quality_dropped == 85

    def test_clean_page_reports_nothing_dropped(self, tmp_path, monkeypatch):
        engine = _stub_engine(monkeypatch, tmp_path, _tsv(good=12, noise=0))

        result = engine.recognize_page("x.pdf", 1)

        assert len(result.words) == 12
        assert result.low_quality_dropped == 0
