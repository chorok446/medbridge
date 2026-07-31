"""Tesseract 5 어댑터 — TSV 출력으로 단어·좌표·계층·confidence를 얻는다.

바이너리 탐색 순서:
1. MEDBRIDGE_OCR_DIR (Tauri 리소스: tesseract.exe + tessdata/) — 시스템 PATH 비의존
2. 개발 환경 fallback: PATH의 tesseract
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.services.extraction.geometry import clamp_bbox
from app.services.extraction.ocr import OcrResult, OcrWord
from app.services.ocr.render import render_page
from app.services.ocr.settings import (
    OCR_DEFAULT_DPI,
    OCR_MIN_WORD_CONFIDENCE,
    OCR_TIMEOUT_SECONDS,
)

logger = get_logger(__name__)


@dataclass
class TsvWord:
    text: str
    confidence: float  # 0.0~1.0
    left: int
    top: int
    width: int
    height: int
    block_index: int
    paragraph_index: int
    line_index: int
    word_index: int


def parse_tsv(tsv: str) -> list[TsvWord]:
    """Tesseract TSV(level 5 = word) 파싱. 신뢰도가 없는(-1) 행과 공백 텍스트는 제외."""
    words: list[TsvWord] = []
    lines = tsv.splitlines()
    for line in lines[1:]:  # 헤더 제외
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            level = int(cols[0])
            if level != 5:
                continue
            conf = float(cols[10])
            text = cols[11]
            if conf < 0 or not text.strip():
                continue
            words.append(
                TsvWord(
                    text=text,
                    confidence=max(0.0, min(1.0, conf / 100.0)),
                    left=int(cols[6]),
                    top=int(cols[7]),
                    width=int(cols[8]),
                    height=int(cols[9]),
                    block_index=int(cols[2]),
                    paragraph_index=int(cols[3]),
                    line_index=int(cols[4]),
                    word_index=int(cols[5]),
                )
            )
        except (ValueError, IndexError):
            continue
    return words


def pixels_to_pdf(
    word: TsvWord, effective_dpi: int, page_width_pt: float, page_height_pt: float
) -> tuple[float, float, float, float]:
    """픽셀 → PDF 시각 공간(pt). 렌더링이 회전을 반영하므로 스케일 변환만 필요하다."""
    scale = 72.0 / effective_dpi
    return clamp_bbox(
        (
            word.left * scale,
            word.top * scale,
            (word.left + word.width) * scale,
            (word.top + word.height) * scale,
        ),
        page_width_pt,
        page_height_pt,
    )


class TesseractEngine:
    def __init__(self) -> None:
        self._binary: str | None = None
        self._tessdata: str | None = None
        self._version = ""
        self._locate()

    def _locate(self) -> None:
        ocr_dir = os.environ.get("MEDBRIDGE_OCR_DIR")
        if ocr_dir:
            candidate = Path(ocr_dir) / (
                "tesseract.exe" if os.name == "nt" else "tesseract"
            )
            if candidate.is_file():
                self._binary = str(candidate)
                tessdata = Path(ocr_dir) / "tessdata"
                if tessdata.is_dir():
                    self._tessdata = str(tessdata)
        if self._binary is None:
            self._binary = shutil.which("tesseract")  # 개발 환경 fallback
        if self._binary:
            try:
                out = subprocess.run(
                    [self._binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                first = (out.stdout or out.stderr).splitlines()[0]
                self._version = first.replace("tesseract", "").strip()
            except Exception:
                self._binary = None

    @property
    def available(self) -> bool:
        return self._binary is not None

    @property
    def version(self) -> str:
        return self._version

    def recognize_page(
        self,
        pdf_path: str,
        page_number: int,
        *,
        language: str = "kor+eng",
        dpi: int = 0,
    ) -> OcrResult:
        if not self.available:
            raise RuntimeError("tesseract binary not found")
        dpi = dpi or OCR_DEFAULT_DPI
        started = time.monotonic()
        rendered = render_page(pdf_path, page_number, dpi)
        try:
            env = dict(os.environ)
            if self._tessdata:
                env["TESSDATA_PREFIX"] = self._tessdata
            def run_psm(psm: str) -> list[TsvWord]:
                cmd = [
                    self._binary or "tesseract",
                    str(rendered.path),
                    "stdout",
                    "-l",
                    language,
                    "--psm",
                    psm,
                    "tsv",
                ]
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        env=env,
                        timeout=OCR_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired as exc:
                    # subprocess.run이 timeout 시 하위 프로세스를 강제 종료한다
                    raise TimeoutError(f"ocr timeout ({OCR_TIMEOUT_SECONDS}s)") from exc
                if proc.returncode != 0:
                    raise RuntimeError(f"tesseract exited {proc.returncode}")
                stderr = proc.stderr.decode("utf-8", errors="replace")
                if "Can't open tsv" in stderr:
                    # tessdata/configs/tsv 누락 — 조용한 빈 결과 대신 명시적 실패
                    raise RuntimeError("tesseract tsv config missing")
                stdout = proc.stdout.decode("utf-8", errors="replace")
                return [
                    w for w in parse_tsv(stdout) if w.confidence >= OCR_MIN_WORD_CONFIDENCE
                ]

            tsv_words = run_psm("3")
            psm_fallback = False
            if len(tsv_words) < 3:
                # 자동 분할(psm 3)이 희소 페이지를 버리는 경우 → 단일 블록 모드 재시도
                retry = run_psm("6")
                if len(retry) > len(tsv_words):
                    tsv_words = retry
                    psm_fallback = True
            words: list[OcrWord] = []
            for w in tsv_words:
                pdf_box = pixels_to_pdf(
                    w, rendered.effective_dpi, rendered.page_width_pt, rendered.page_height_pt
                )
                words.append(
                    OcrWord(
                        bbox=pdf_box,
                        text=w.text,
                        confidence=w.confidence,
                        pixel_x=w.left,
                        pixel_y=w.top,
                        pixel_width=w.width,
                        pixel_height=w.height,
                        pdf_x0=pdf_box[0],
                        pdf_y0=pdf_box[1],
                        pdf_x1=pdf_box[2],
                        pdf_y1=pdf_box[3],
                        block_index=w.block_index,
                        paragraph_index=w.paragraph_index,
                        line_index=w.line_index,
                        word_index=w.word_index,
                    )
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            mean_conf = (
                sum(w.confidence for w in words) / len(words) if words else 0.0
            )
            return OcrResult(
                page_number=page_number,
                words=words,
                full_text=_assemble_text(words),
                language=language,
                engine="tesseract",
                engine_version=self._version,
                mean_confidence=round(mean_conf, 4),
                render_dpi=rendered.effective_dpi,
                duration_ms=duration_ms,
                warnings=rendered.warnings + (["psm_fallback"] if psm_fallback else []),
            )
        finally:
            rendered.path.unlink(missing_ok=True)  # 임시 이미지 정리


def _assemble_text(words: list[OcrWord]) -> str:
    """TSV 계층(블록→문단→줄) 순서로 텍스트 조립."""
    parts: list[str] = []
    current_key: tuple[int, int, int] | None = None
    line_words: list[str] = []
    for w in sorted(
        words, key=lambda w: (w.block_index, w.paragraph_index, w.line_index, w.word_index)
    ):
        key = (w.block_index, w.paragraph_index, w.line_index)
        if current_key is not None and key != current_key:
            parts.append(" ".join(line_words))
            line_words = []
        current_key = key
        line_words.append(w.text)
    if line_words:
        parts.append(" ".join(line_words))
    return "\n".join(parts)


_engine: TesseractEngine | None = None


def get_tesseract_engine() -> TesseractEngine:
    global _engine
    if _engine is None:
        _engine = TesseractEngine()
    return _engine


def reset_engine_cache() -> None:
    global _engine
    _engine = None
