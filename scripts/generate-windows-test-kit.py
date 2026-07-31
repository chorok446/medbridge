#!/usr/bin/env python3
"""Windows 실기기 검증용 테스트 PDF 키트 생성 — 전부 합성(실제 환자 정보 없음).

docs/testing/windows-test-kit-guide.md 의 항목별 매핑과 함께 사용한다.
출력은 git에 커밋하지 않는다(용량 + 재생성 가능) — 필요할 때 이 스크립트로 다시 만든다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

import pymupdf  # noqa: E402

from tests import extraction_fixtures as fx  # noqa: E402

OUT = ROOT / "docs/testing/windows-test-kit"


def save(name: str, data: bytes) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_bytes(data)
    print(f"{name}: {len(data) / 1024:.0f} KB")


def main() -> None:
    save("01_digital_simple.pdf", fx.single_column_korean(pages=2))
    save("02_two_column_paper.pdf", fx.two_column_english(pages=3))
    save("03_table_doc.pdf", fx.with_table())
    save("04_rotated_page.pdf", fx.rotated_page())
    save("05_scanned_korean_short.pdf", fx.scanned_korean_clear(pages=2))
    save("06_scanned_korean_long.pdf", fx.scanned_korean_clear(pages=6))

    mixed = pymupdf.open()
    for _ in range(8):
        d = pymupdf.open(stream=fx.two_column_english(pages=3), filetype="pdf")
        mixed.insert_pdf(d)
        d.close()
    for _ in range(7):
        d = pymupdf.open(stream=fx.single_column_korean(pages=3), filetype="pdf")
        mixed.insert_pdf(d)
        d.close()
    for _ in range(3):
        d = pymupdf.open(stream=fx.scanned_korean_clear(pages=2), filetype="pdf")
        mixed.insert_pdf(d)
        d.close()
    data = mixed.tobytes()
    print(f"07_mixed_50pages.pdf page count: {mixed.page_count}")
    mixed.close()
    save("07_mixed_50pages.pdf", data)


if __name__ == "__main__":
    main()
