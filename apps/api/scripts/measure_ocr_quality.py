#!/usr/bin/env python
"""OCR 품질 측정기 — 저신뢰 페이지가 DPI·psm을 바꾸면 살아나는지 잰다.

전처리(deskew·denoise·threshold)를 도입할지 판단하려면 먼저 **무엇이 문제인지**
알아야 한다. 실기기 문서에서 27쪽 중 13쪽이 저신뢰로 나왔지만, 원인이 기울어짐인지
원본 해상도인지 인식 언어인지 구분할 방법이 없었다. 이 스크립트는 같은 쪽을
DPI × psm 조합으로 돌려 신뢰도가 어떻게 움직이는지 표로 보여준다.

의존성을 새로 추가하지 않는다. 판정은 앱의 실제 코드(`render_page`, `parse_tsv`,
`classify_result`, `OCR_MIN_WORD_CONFIDENCE`)를 그대로 쓴다 — 따로 구현하면 앱이
내리는 판정이 아니라 다른 것을 재게 된다.

예:
  # DB에 있는 문서의 저신뢰 쪽만
  uv run python scripts/measure_ocr_quality.py --document-id <uuid>

  # 파일을 직접 지정
  uv run python scripts/measure_ocr_quality.py --pdf book.pdf --pages 3,7,12

  # 조합을 넓혀서
  uv run python scripts/measure_ocr_quality.py --pdf book.pdf --pages 3 --dpi 300,400,600

읽기 전용이다. DB에 쓰지 않고 원본 PDF도 수정하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 콘솔 기본 코드페이지(cp949)로는 이 스크립트의 한글 출력과 표 괘선이 깨진다.
# 측정 결과를 사람이 읽는 것이 목적이므로 출력 인코딩을 UTF-8로 고정한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.models.enums import OcrRunStatus  # noqa: E402
from app.services.extraction.ocr import OcrResult, OcrWord  # noqa: E402
from app.services.ocr.render import render_page  # noqa: E402
from app.services.ocr.service import classify_result  # noqa: E402
from app.services.ocr.settings import OCR_MIN_WORD_CONFIDENCE  # noqa: E402
from app.services.ocr.tesseract import (  # noqa: E402
    get_tesseract_engine,
    parse_tsv,
    pixels_to_pdf,
)

# 저신뢰로 분류돼 청크에서 빠지는 상태들. 재시도 라우트(`/ocr/retry`)가 고르는 것과
# 같은 집합이라, 여기서 재는 대상이 실제 재시도 대상과 어긋나지 않는다.
RETRY_STATUSES = ("ocr_failed", "ocr_empty", "ocr_low_confidence", "ocr_cancelled")


@dataclass
class Measurement:
    """한 (쪽, DPI, psm) 조합의 결과."""

    page: int
    dpi: int
    psm: str
    effective_dpi: int
    kept: int  # 임계값을 넘겨 저장 후보가 된 단어
    dropped: int  # 임계값 미만이라 버린 단어
    mean_confidence: float
    low_ratio: float
    median: float
    status: str
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def raw(self) -> int:
        return self.kept + self.dropped

    @property
    def drop_rate(self) -> float:
        return self.dropped / self.raw if self.raw else 0.0


def run_once(engine, pdf_path: str, page: int, dpi: int, psm: str, language: str) -> Measurement:
    """한 조합을 실행한다. 엔진의 psm 자동 fallback을 거치지 않고 psm을 고정한다."""
    blank = Measurement(
        page=page, dpi=dpi, psm=psm, effective_dpi=0, kept=0, dropped=0,
        mean_confidence=0.0, low_ratio=1.0, median=0.0, status="-",
    )
    try:
        rendered = render_page(pdf_path, page, dpi)
    except Exception as exc:
        blank.error = f"render: {type(exc).__name__}"
        return blank

    try:
        env = dict(os.environ)
        tessdata = getattr(engine, "_tessdata", None)
        if tessdata:
            env["TESSDATA_PREFIX"] = tessdata
        proc = subprocess.run(
            [
                getattr(engine, "_binary", "tesseract"), str(rendered.path), "stdout",
                "-l", language, "--psm", psm, "tsv",
            ],
            capture_output=True,
            env=env,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            tail = " ".join(proc.stderr.decode("utf-8", "replace").split())[-120:]
            blank.error = f"exit {proc.returncode}: {tail}"
            blank.effective_dpi = rendered.effective_dpi
            return blank

        parsed = parse_tsv(proc.stdout.decode("utf-8", "replace"))
        kept = [w for w in parsed if w.confidence >= OCR_MIN_WORD_CONFIDENCE]

        # 앱과 같은 판정을 얻으려면 앱이 보는 것과 같은 모양으로 만들어야 한다.
        words = [
            OcrWord(
                bbox=pixels_to_pdf(
                    w, rendered.effective_dpi, rendered.page_width_pt, rendered.page_height_pt
                ),
                text=w.text,
                confidence=w.confidence,
                block_index=w.block_index,
                paragraph_index=w.paragraph_index,
                line_index=w.line_index,
                word_index=w.word_index,
            )
            for w in kept
        ]
        mean = sum(w.confidence for w in words) / len(words) if words else 0.0
        status, low_ratio, median = classify_result(
            OcrResult(page_number=page, words=words, mean_confidence=mean)
        )
        return Measurement(
            page=page,
            dpi=dpi,
            psm=psm,
            effective_dpi=rendered.effective_dpi,
            kept=len(kept),
            dropped=len(parsed) - len(kept),
            mean_confidence=round(mean, 4),
            low_ratio=round(low_ratio, 4),
            median=round(median, 4),
            status=status.value,
            warnings=list(rendered.warnings),
        )
    except subprocess.TimeoutExpired:
        blank.error = "timeout"
        return blank
    except Exception as exc:
        blank.error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:100]}"
        return blank
    finally:
        rendered.path.unlink(missing_ok=True)


async def pages_from_db(
    document_id: str, only_bad: bool
) -> tuple[str, list[int], dict[int, str]]:
    """DB에서 원본 경로와 대상 쪽을 읽는다. 읽기 전용."""
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.document import Document
    from app.models.extraction import DocumentPage
    from app.services.documents import storage

    doc_uuid = uuid.UUID(document_id)
    async with get_session_factory()() as session:
        doc = await session.get(Document, doc_uuid)
        if doc is None or doc.storage_key is None:
            raise SystemExit(f"문서를 찾을 수 없습니다: {document_id}")
        stmt = select(DocumentPage).where(DocumentPage.document_id == doc_uuid)
        if only_bad:
            stmt = stmt.where(DocumentPage.ocr_status.in_(RETRY_STATUSES))
        rows = list(
            (await session.execute(stmt.order_by(DocumentPage.page_number))).scalars()
        )
        path = str(storage.get_storage().resolve_path(doc.storage_key))
        return (
            path,
            [p.page_number for p in rows],
            {p.page_number: p.ocr_status or "-" for p in rows},
        )


def print_table(results: list[Measurement], baseline: tuple[int, str]) -> None:
    by_page: dict[int, list[Measurement]] = {}
    for m in results:
        by_page.setdefault(m.page, []).append(m)

    print()
    print(f"{'쪽':>4} {'DPI':>5} {'psm':>4} {'단어':>6} {'버림':>6} {'버림%':>6} "
          f"{'평균신뢰':>8} {'저신뢰비':>8} {'판정':>18}  비고")
    print("-" * 96)

    improved = stuck = fine = 0
    for page in sorted(by_page):
        base = next(
            (m for m in by_page[page] if (m.dpi, m.psm) == baseline), by_page[page][0]
        )
        for m in sorted(by_page[page], key=lambda m: (m.dpi, m.psm)):
            note = []
            if m.error:
                note.append(m.error)
            if m.effective_dpi and m.effective_dpi != m.dpi:
                note.append(f"DPI하향→{m.effective_dpi}")
            note += [w for w in m.warnings if not w.startswith("dpi_reduced")]
            mark = " "
            if m is not base and not m.error and not base.error:
                delta = m.mean_confidence - base.mean_confidence
                mark = "+" if delta > 0.02 else ("-" if delta < -0.02 else " ")
                note.append(f"Δ{delta:+.3f}")
            print(
                f"{m.page:>4} {m.dpi:>5} {m.psm:>4} {m.kept:>6} {m.dropped:>6} "
                f"{m.drop_rate * 100:>5.0f}% {m.mean_confidence:>8.3f} {m.low_ratio:>8.3f} "
                f"{m.status:>18}{mark} {' '.join(note)}"
            )

        # 이 쪽이 어떤 조합으로든 정상 판정에 도달하는가
        rescued = [
            m for m in by_page[page]
            if m.status == OcrRunStatus.OCR_COMPLETED.value and m is not base
        ]
        if base.status == OcrRunStatus.OCR_COMPLETED.value:
            fine += 1
        elif rescued:
            best = max(rescued, key=lambda m: m.mean_confidence)
            print(f"     └ 살아남: {best.dpi}dpi psm{best.psm} 로 {base.status} → {best.status}")
            improved += 1
        else:
            stuck += 1
        print()

    print("=" * 96)
    total_bad = improved + stuck
    print(f"기준 조합에서 저신뢰/실패였던 쪽: {total_bad}")
    print(f"  설정 변경만으로 정상 판정 도달: {improved}")
    print(f"  어떤 조합으로도 못 살림:        {stuck}")
    print(f"  원래 정상이던 쪽:               {fine}")
    print()
    if total_bad == 0:
        print("판단: 이 표본에는 저신뢰 쪽이 없다. 대상을 다시 고르라(--all-pages 를 뺐는지 확인).")
    elif stuck == 0:
        print("판단: 전처리 불필요. DPI·psm 재시도만으로 전부 회복된다.")
        print("      `/ocr/retry`가 이미 400dpi로 재시도하므로, 남은 일은 psm 고정 여부뿐이다.")
    elif improved == 0:
        print("판단: 설정으로는 안 된다. 원본 이미지가 문제다 — 전처리를 검토할 값어치가 있다.")
        print("      다음 단계는 bbox를 움직이지 않는 것부터(adaptive threshold·그림자 제거).")
        print("      deskew는 좌표 역변환을 함께 설계할 때만. pixels_to_pdf는 배율 변환뿐이라")
        print("      회전을 되돌리지 못하고, 하이라이트와 중복 제거가 어긋난다.")
    else:
        print(f"판단: 절반의 문제다. {improved}쪽은 설정으로 회복되고 {stuck}쪽은 아니다.")
        print("      회복되지 않는 쪽만 따로 보고 전처리 여부를 정하라.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="측정할 PDF 경로")
    src.add_argument("--document-id", help="DB에 있는 문서 UUID")
    ap.add_argument("--pages", help="쉼표로 구분한 쪽 번호. 생략 시 저신뢰 쪽 전부(--document-id)")
    ap.add_argument("--dpi", default="300,400", help="쉼표로 구분한 DPI 목록 (기본: 300,400)")
    ap.add_argument("--psm", default="3,6", help="쉼표로 구분한 psm 목록 (기본: 3,6)")
    ap.add_argument("--language", default="kor+eng")
    ap.add_argument("--all-pages", action="store_true", help="저신뢰뿐 아니라 모든 쪽")
    ap.add_argument("--json", help="결과를 JSON으로 저장할 경로")
    args = ap.parse_args()

    engine = get_tesseract_engine()
    if not engine.available:
        print(
            "tesseract를 찾지 못했습니다. MEDBRIDGE_OCR_DIR 또는 PATH를 확인하세요.",
            file=sys.stderr,
        )
        return 2
    print(f"엔진: tesseract {engine.version}")

    statuses: dict[int, str] = {}
    if args.document_id:
        pdf_path, pages, statuses = asyncio.run(
            pages_from_db(args.document_id, only_bad=not args.all_pages)
        )
        if args.pages:
            pages = [int(p) for p in args.pages.split(",")]
    else:
        pdf_path = args.pdf
        if not args.pages:
            print("--pdf 를 쓸 때는 --pages 가 필요합니다.", file=sys.stderr)
            return 2
        pages = [int(p) for p in args.pages.split(",")]

    if not pages:
        print("측정할 쪽이 없습니다. (저신뢰 쪽이 하나도 없거나 문서가 비어 있음)")
        return 0

    dpis = [int(d) for d in args.dpi.split(",")]
    psms = [p.strip() for p in args.psm.split(",")]
    if statuses:
        print("대상: " + ", ".join(f"{p}쪽({statuses.get(p, '-')})" for p in pages))
    else:
        print(f"대상: {len(pages)}쪽")
    print(
        f"조합: DPI {dpis} × psm {psms} = 쪽당 {len(dpis) * len(psms)}회, "
        f"총 {len(pages) * len(dpis) * len(psms)}회"
    )

    results: list[Measurement] = []
    for page in pages:
        for dpi in dpis:
            for psm in psms:
                print(f"  ... {page}쪽 {dpi}dpi psm{psm}", end="\r", flush=True)
                results.append(run_once(engine, pdf_path, page, dpi, psm, args.language))
    print(" " * 40, end="\r")

    print_table(results, baseline=(dpis[0], psms[0]))

    if args.json:
        Path(args.json).write_text(
            json.dumps([vars(m) for m in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON 저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
