import os
import tempfile

# Settings가 import 시점에 캐시되므로 테스트 환경 변수를 가장 먼저 주입한다.
# 앱 데이터(DB·문서 파일)는 저장소 밖 임시 디렉터리에 격리된다.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="medbridge-test-")
os.environ["APP_ENV"] = "test"
os.environ["MEDBRIDGE_APP_DATA_DIR"] = _TEST_DATA_DIR
# .env 파일 값이 새어들지 않도록 빈 값으로 가린다 (빈 문자열 → override 미사용)
os.environ["MEDBRIDGE_API_TOKEN"] = ""
os.environ["DATABASE_URL"] = ""

import pytest


def make_pdf(pages: int = 1) -> bytes:
    """pypdf로 읽을 수 있는 최소 유효 PDF 생성."""
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_encrypted_pdf() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    return make_pdf()
