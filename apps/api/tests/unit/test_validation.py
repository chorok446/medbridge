import hashlib

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.documents import validation
from tests.conftest import make_encrypted_pdf, make_pdf


class TestSignature:
    def test_valid_pdf_signature(self):
        validation.check_pdf_signature(b"%PDF-1.7")

    def test_non_pdf_rejected(self):
        with pytest.raises(AppError) as exc:
            validation.check_pdf_signature(b"PK\x03\x04")  # zip/docx
        assert exc.value.code == ErrorCode.INVALID_FILE_TYPE

    def test_renamed_text_file_rejected(self):
        """확장자가 .pdf여도 실제 시그니처가 아니면 거부."""
        with pytest.raises(AppError) as exc:
            validation.check_pdf_signature(b"hello wo")
        assert exc.value.code == ErrorCode.INVALID_FILE_TYPE


class TestSize:
    def test_within_limit(self):
        validation.check_size(100, max_bytes=1000)

    def test_over_limit(self):
        with pytest.raises(AppError) as exc:
            validation.check_size(1001, max_bytes=1000)
        assert exc.value.code == ErrorCode.FILE_TOO_LARGE
        assert exc.value.status_code == 413

    def test_empty_file(self):
        with pytest.raises(AppError) as exc:
            validation.check_size(0, max_bytes=1000)
        assert exc.value.code == ErrorCode.EMPTY_PDF


class TestSha256:
    def test_matches_hashlib(self):
        data = b"medbridge test data"
        assert validation.compute_sha256(data) == hashlib.sha256(data).hexdigest()


class TestInspectPdf:
    def test_valid_pdf(self):
        data = make_pdf(pages=3)
        result = validation.inspect_pdf(data)
        assert result.page_count == 3
        assert result.sha256 == validation.compute_sha256(data)
        assert result.file_size == len(data)

    def test_valid_pdf_path_uses_file_handle(self, tmp_path):
        data = make_pdf(pages=2)
        path = tmp_path / "large-style.pdf"
        path.write_bytes(data)

        result = validation.inspect_pdf_path(path)

        assert result.page_count == 2
        assert result.sha256 == hashlib.sha256(data).hexdigest()
        assert result.file_size == path.stat().st_size

    def test_encrypted_pdf_rejected(self):
        with pytest.raises(AppError) as exc:
            validation.inspect_pdf(make_encrypted_pdf())
        assert exc.value.code == ErrorCode.ENCRYPTED_PDF

    def test_corrupted_pdf_rejected(self):
        data = make_pdf()[:60] + b"\x00garbage-truncated"
        with pytest.raises(AppError) as exc:
            validation.inspect_pdf(data)
        assert exc.value.code == ErrorCode.CORRUPTED_PDF

    def test_non_pdf_rejected(self):
        with pytest.raises(AppError) as exc:
            validation.inspect_pdf(b"not a pdf at all" * 10)
        assert exc.value.code == ErrorCode.INVALID_FILE_TYPE

    def test_error_message_hides_internals(self):
        """내부 파서 예외 메시지를 사용자 메시지로 노출하지 않는다."""
        try:
            validation.inspect_pdf(b"%PDF-1.4 broken")
        except AppError as exc:
            assert "Traceback" not in exc.message
            assert "pypdf" not in exc.message.lower()
