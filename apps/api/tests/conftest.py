import os
import uuid

# Settings가 기동 시 검증되므로 테스트 환경 변수를 import 전에 주입한다
os.environ["APP_ENV"] = "test"
os.environ["APP_MODE"] = "single_user"
os.environ["LOCAL_USER_EMAIL"] = "local-test@example.com"
os.environ["LOCAL_USER_DISPLAY_NAME"] = "테스트 사용자"
os.environ["SECRET_KEY"] = "test-secret-key-must-be-32-chars-long!!"
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://medbridge:medbridge@localhost:5432/medbridge_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("MINIO_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "medbridge")
os.environ.setdefault("MINIO_SECRET_KEY", "medbridge-secret")
os.environ.setdefault("MINIO_BUCKET_ORIGINALS", "medbridge-test-originals")

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


@pytest.fixture
def unique_email() -> str:
    return f"test-{uuid.uuid4().hex[:10]}@example.com"
