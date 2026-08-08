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


def strip_extended_prefix(path: str | None) -> str | None:
    r"""Windows 확장 길이 경로 접두사(`\\?\`)를 제거한다.

    Tauri의 resource_dir()는 Windows에서 `\\?\C:\...` 형태를 돌려주고, 그 값이 그대로
    MEDBRIDGE_OCR_DIR → TESSDATA_PREFIX로 흘러간다. Python은 이 형태를 문제없이 다루므로
    바이너리 탐지도 `tesseract --version`도 성공하지만, tesseract(leptonica)는
    `TESSDATA_PREFIX + "/" + lang + ".traineddata"`를 이어 붙여
    `\\?\C:\...\tessdata/eng.traineddata`를 만든다. Win32는 `\\?\` 경로를 **정규화하지
    않으므로** 섞여 들어간 슬래시 때문에 파일을 열지 못한다.

    실기기 증상: 45페이지 전량이 `tesseract exited 1: ... Failed loading language 'eng'
    ... Tesseract couldn't load any languages!`로 실패. 같은 바이너리·같은 tessdata를
    평범한 경로로 부르면 정상 동작한다.

    UNC(`\\?\UNC\server\share`)는 `\\server\share`로 되돌린다.
    """
    if not path:
        return path
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\") :]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\") :]
    return path


# 패키징된 앱에서 tesseract를 부를 때 콘솔 창이 뜨지 않게 한다.
#
# sidecar.spec이 `console=False`라 sidecar 프로세스에는 콘솔이 없다. 그 상태에서
# 콘솔 앱인 tesseract.exe를 그냥 spawn하면 Windows가 **자식마다 새 콘솔을 할당**해
# 검은 창이 화면에 번쩍인다. capture_output(파이프 리다이렉트)로는 막히지 않는다 —
# 창 생성 자체를 끄는 플래그가 따로 필요하다. 27쪽 문서면 최대 55번 깜빡인다.
# DESIGN.md: "개발자 도구처럼 보이게 하지 않는다".
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tail(text: str, limit: int = 300) -> str:
    """오류 문구 꼬리만 한 줄로. 원문 길이가 로그를 잡아먹지 않게 자른다."""
    flat = " ".join(text.split())
    return flat[-limit:] if len(flat) > limit else flat


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
        ocr_dir = strip_extended_prefix(os.environ.get("MEDBRIDGE_OCR_DIR"))
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
                    creationflags=_NO_WINDOW,
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

    def describe(self) -> dict:
        """엔진 상태 스냅샷 — 잡 시작 시 한 번 남겨 '무엇이 없어서' 실패했는지 좁힌다."""
        tessdata = Path(self._tessdata) if self._tessdata else None
        langs = (
            sorted(p.stem for p in tessdata.glob("*.traineddata")) if tessdata else []
        )
        return {
            "binary": self._binary or "",
            "version": self._version,
            "tessdata": self._tessdata or "",
            "tessdata_langs": ",".join(langs),
            "tsv_config": bool(tessdata and (tessdata / "configs" / "tsv").is_file()),
            "ocr_dir_env": bool(os.environ.get("MEDBRIDGE_OCR_DIR")),
        }

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
        try:
            rendered = render_page(pdf_path, page_number, dpi)
        except Exception as exc:
            # 렌더 실패와 인식 실패는 대응이 완전히 다르다(파일 손상 vs 엔진 문제).
            # 둘 다 RuntimeError로 뭉뚱그려지면 로그만으로 구분할 수 없다.
            raise RuntimeError(
                f"page render failed ({type(exc).__name__}): {_tail(str(exc))}"
            ) from exc
        try:
            env = dict(os.environ)
            if self._tessdata:
                env["TESSDATA_PREFIX"] = self._tessdata
            def run_psm(psm: str) -> tuple[list[TsvWord], int]:
                """반환: (임계값을 넘긴 단어, 임계값 미만이라 버린 단어 수)."""
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
                        creationflags=_NO_WINDOW,
                    )
                except subprocess.TimeoutExpired as exc:
                    # subprocess.run이 timeout 시 하위 프로세스를 강제 종료한다
                    raise TimeoutError(f"ocr timeout ({OCR_TIMEOUT_SECONDS}s)") from exc
                stderr = proc.stderr.decode("utf-8", errors="replace")
                if proc.returncode != 0:
                    # stderr를 함께 담는다. 종료 코드만으로는 언어 로드 실패·이미지 읽기
                    # 실패·DLL 문제를 구분할 수 없어, 실기기에서 45페이지가 전부 같은
                    # "RuntimeError"로만 기록되고 원인을 좁힐 단서가 0이었다.
                    # tesseract stderr에는 문서 본문이 들어가지 않는다(경고·오류 문구뿐).
                    raise RuntimeError(
                        f"tesseract exited {proc.returncode}: {_tail(stderr)}"
                    )
                if "Can't open tsv" in stderr:
                    # tessdata/configs/tsv 누락 — 조용한 빈 결과 대신 명시적 실패
                    raise RuntimeError(f"tesseract tsv config missing: {_tail(stderr)}")
                stdout = proc.stdout.decode("utf-8", errors="replace")
                # 버린 몫을 함께 돌려준다. 필터가 여기서 걸리므로, 세지 않으면 그
                # 사실이 이 함수 밖으로 나가지 못하고 하류의 어떤 지표에도 남지 않는다.
                parsed = parse_tsv(stdout)
                kept = [w for w in parsed if w.confidence >= OCR_MIN_WORD_CONFIDENCE]
                return kept, len(parsed) - len(kept)

            tsv_words, dropped = run_psm("3")
            psm_fallback = False
            if len(tsv_words) < 3:
                # 자동 분할(psm 3)이 희소 페이지를 버리는 경우 → 단일 블록 모드 재시도
                retry, retry_dropped = run_psm("6")
                if len(retry) > len(tsv_words):
                    tsv_words = retry
                    # 채택한 실행의 값으로 갈아 끼운다 — 버린 몫은 psm마다 다르다.
                    dropped = retry_dropped
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
                low_quality_dropped=dropped,
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
