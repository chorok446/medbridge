"""PDF 파일 검증. 순수 함수로 유지해 단위 테스트 가능하게 한다."""

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import AppError, ErrorCode

PDF_SIGNATURE = b"%PDF-"
_HASH_CHUNK = 1024 * 1024


@dataclass
class PdfInspection:
    page_count: int
    sha256: str
    file_size: int


def check_size(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise AppError(
            ErrorCode.FILE_TOO_LARGE,
            f"파일이 최대 크기({max_bytes // (1024 * 1024)}MB)를 초과했습니다.",
            status_code=413,
        )
    if size == 0:
        raise AppError(ErrorCode.EMPTY_PDF, "빈 파일은 업로드할 수 없습니다.", status_code=400)


def check_pdf_signature(head: bytes | bytearray) -> None:
    """확장자가 아닌 실제 파일 시그니처(%PDF-)를 검사한다."""
    if not head.startswith(PDF_SIGNATURE):
        raise AppError(
            ErrorCode.INVALID_FILE_TYPE,
            "PDF 파일만 업로드할 수 있습니다.",
            status_code=415,
        )


def compute_sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_metadata(source: BinaryIO) -> tuple[int, str]:
    """고정 크기 버퍼로 signature, 실제 바이트 수와 SHA-256을 계산한다."""
    digest = hashlib.sha256()
    file_size = 0
    try:
        source.seek(0)
        head = source.read(8)
        check_pdf_signature(head)
        digest.update(head)
        file_size = len(head)
        while chunk := source.read(_HASH_CHUNK):
            file_size += len(chunk)
            digest.update(chunk)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "파일을 읽는 중 오류가 발생했습니다. 다시 시도해 주세요.",
        ) from exc
    return file_size, digest.hexdigest()


def _inspect_pdf_file(source: BinaryIO) -> PdfInspection:
    """열린 파일 핸들에서 전체 검증을 수행하되 전체 bytes를 만들지 않는다.

    실패는 안정적인 오류 코드를 가진 AppError로 변환한다.
    내부 파서 예외 메시지는 사용자에게 노출하지 않는다.
    """
    file_size, sha256 = _read_metadata(source)
    try:
        source.seek(0)
        reader = PdfReader(source)
    except PdfReadError as exc:
        raise AppError(ErrorCode.CORRUPTED_PDF, "손상된 PDF 파일입니다.", status_code=400) from exc
    except Exception as exc:  # pypdf는 손상 파일에서 다양한 예외를 던진다
        raise AppError(
            ErrorCode.CORRUPTED_PDF, "PDF 파일을 읽을 수 없습니다.", status_code=400
        ) from exc

    if reader.is_encrypted:
        raise AppError(
            ErrorCode.ENCRYPTED_PDF,
            "암호화된 PDF는 업로드할 수 없습니다. 암호를 해제한 뒤 다시 업로드해 주세요.",
            status_code=400,
        )

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise AppError(
            ErrorCode.CORRUPTED_PDF, "PDF 페이지 구조가 손상되었습니다.", status_code=400
        ) from exc

    if page_count == 0:
        raise AppError(ErrorCode.EMPTY_PDF, "페이지가 없는 PDF입니다.", status_code=400)

    return PdfInspection(page_count=page_count, sha256=sha256, file_size=file_size)


def inspect_pdf_file(source: BinaryIO) -> PdfInspection:
    """호출자가 소유한 seekable binary file을 검증한다. 핸들은 닫지 않는다."""
    return _inspect_pdf_file(source)


def inspect_pdf_path(path: Path) -> PdfInspection:
    """경로 기반 검증. 앱 저장소가 안전하게 resolve한 경로만 호출부에서 전달한다."""
    try:
        with path.open("rb") as source:
            return _inspect_pdf_file(source)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "파일을 읽는 중 오류가 발생했습니다. 다시 시도해 주세요.",
        ) from exc


def inspect_pdf(data: bytes) -> PdfInspection:
    """기존 소형 bytes 호출용 호환 API."""
    return _inspect_pdf_file(BytesIO(data))
