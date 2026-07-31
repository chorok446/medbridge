"""추출 테스트용 PDF fixture 생성기 — 전부 코드로 생성 (저작권·개인정보 없음)."""

import pymupdf

PAGE_W, PAGE_H = 595, 842  # A4 (pt)


KOR = {"fontname": "korea"}


def _new_doc() -> pymupdf.Document:
    return pymupdf.open()


def _to_bytes(doc: pymupdf.Document) -> bytes:
    data = doc.tobytes()
    doc.close()
    return data


def single_column_korean(pages: int = 2) -> bytes:
    doc = _new_doc()
    for n in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 60), f"심부전의 이해 - {n + 1}장", fontsize=16, **KOR)
        y = 100
        for i in range(12):
            page.insert_text(
                (72, y),
                f"문단 {i + 1}. 심장은 온몸에 혈액을 보내는 근육 기관이다.",
                fontsize=11,
                **KOR,
            )
            y += 24
        # 반복 꼬리말 (페이지 번호)
        page.insert_text((PAGE_W / 2 - 10, PAGE_H - 30), f"- {n + 1} -", fontsize=9)
    return _to_bytes(doc)


def two_column_english(pages: int = 3) -> bytes:
    """전체 폭 제목 + 좌우 2단 본문 + 반복 머리말."""
    doc = _new_doc()
    for n in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 40), "Journal of Cardiology Study", fontsize=9)  # 머리말
        if n == 0:
            page.insert_text((72, 90), "Effects of Exercise on Heart Failure Outcomes", fontsize=15)
        y = 140 if n == 0 else 100
        left_text = "\n".join(f"L{n}-{i} left column line {i}" for i in range(14))
        right_text = "\n".join(f"R{n}-{i} right column line {i}" for i in range(14))
        page.insert_textbox(pymupdf.Rect(60, y, 280, y + 420), left_text, fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, y + 7, 540, y + 427), right_text, fontsize=10)
        page.insert_text((PAGE_W / 2 - 8, PAGE_H - 28), str(n + 1), fontsize=9)  # 꼬리말
    return _to_bytes(doc)


def rotated_page() -> bytes:
    doc = _new_doc()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 80), "회전 페이지 테스트 본문입니다.", fontsize=12, **KOR)
    page.set_rotation(90)
    return _to_bytes(doc)


def with_table() -> bytes:
    doc = _new_doc()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 60), "표가 있는 문서", fontsize=14, **KOR)
    # 3x3 표: 셀 경계선 + 텍스트
    x0, y0, cw, rh = 72, 120, 140, 30
    for r in range(4):
        page.draw_line((x0, y0 + r * rh), (x0 + 3 * cw, y0 + r * rh))
    for c in range(4):
        page.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + 3 * rh))
    headers = ["항목", "수치", "단위"]
    rows = [["심박수", "72", "bpm"], ["혈압", "120", "mmHg"]]
    for c, text in enumerate(headers):
        page.insert_text((x0 + c * cw + 6, y0 + 20), text, fontsize=10, **KOR)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            page.insert_text((x0 + c * cw + 6, y0 + (r + 1) * rh + 20), text, fontsize=10, **KOR)
    page.insert_text((72, y0 + 3 * rh + 30), "표 1. 기본 활력 징후 예시", fontsize=9, **KOR)
    return _to_bytes(doc)


def _tiny_png() -> bytes:
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))

    w = h = 8
    raw = b"".join(b"\x00" + b"\x80\x80\x80\xff" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def scanned_page_doc(text_pages: int = 1, scanned_pages: int = 1) -> bytes:
    """일부 페이지만 스캔(전면 이미지)인 혼합 문서."""
    doc = _new_doc()
    for n in range(text_pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = 80
        for i in range(15):
            page.insert_text(
                (72, y + i * 24), f"디지털 텍스트 페이지 {n + 1} 줄 {i + 1}", fontsize=11, **KOR
            )
    png = _tiny_png()
    for _ in range(scanned_pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), stream=png)
    return _to_bytes(doc)


def image_with_caption() -> bytes:
    doc = _new_doc()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 60), "그림이 있는 문서 본문", fontsize=12, **KOR)
    page.insert_image(pymupdf.Rect(72, 100, 300, 260), stream=_tiny_png())
    page.insert_text((72, 280), "그림 1. 심장 구조 모식도", fontsize=9, **KOR)
    return _to_bytes(doc)


def sparse_text() -> bytes:
    doc = _new_doc()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 80), "끝", fontsize=12, **KOR)
    return _to_bytes(doc)
