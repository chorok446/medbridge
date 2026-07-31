"""파일 저장소 계층.

FileStorage 인터페이스 + 로컬 파일시스템 구현(LocalFileStorage).
PDF는 저장소·설치 디렉터리가 아닌 OS 앱 데이터 경로에 저장한다.
경로는 UUID로만 구성하고(원본 파일명 금지), 앱 데이터 밖 접근을 차단한다.
"""

import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from app.core.errors import AppError, ErrorCode
from app.core.paths import get_path_provider


def object_key(document_id: uuid.UUID) -> str:
    """원본 파일명은 절대 키에 쓰지 않는다 (DB 메타데이터로만 보존)."""
    return f"documents/{document_id}/original.pdf"


class FileStorage(Protocol):
    def save_original(self, key: str, data: bytes | bytearray) -> None: ...
    def read_original(self, key: str) -> bytes: ...
    def original_exists(self, key: str) -> bool: ...
    def delete_original(self, key: str) -> None: ...
    def resolve_path(self, key: str) -> Path: ...


class LocalFileStorage:
    """앱 데이터 디렉터리 하위에 원자적으로 저장하는 로컬 구현."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def resolve_path(self, key: str) -> Path:
        """경로 순회 방지: 최종 경로가 반드시 앱 데이터 루트 안이어야 한다."""
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise AppError(
                ErrorCode.VALIDATION_FAILED, "잘못된 파일 경로입니다.", status_code=400
            )
        return path

    def save_original(self, key: str, data: bytes | bytearray) -> None:
        """임시 파일에 쓴 뒤 os.replace로 원자적 이동. 실패 시 임시 파일 정리."""
        target = self.resolve_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, target)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "파일 저장에 실패했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

    def read_original(self, key: str) -> bytes:
        return self.resolve_path(key).read_bytes()

    def original_exists(self, key: str) -> bool:
        return self.resolve_path(key).is_file()

    def delete_original(self, key: str) -> None:
        """원본과 문서 디렉터리를 제거. 실패 시 예외를 올려 호출부가 보상 처리한다."""
        path = self.resolve_path(key)
        if path.is_file():
            path.unlink()
        doc_dir = path.parent
        if doc_dir != self._root and doc_dir.is_dir() and not any(doc_dir.iterdir()):
            doc_dir.rmdir()


_storage: LocalFileStorage | None = None


def get_storage() -> LocalFileStorage:
    global _storage
    if _storage is None:
        provider = get_path_provider()
        provider.ensure_directories()
        _storage = LocalFileStorage(provider.root)
    return _storage


def reset_storage_cache() -> None:
    """테스트에서 앱 데이터 경로 변경 후 재초기화용."""
    global _storage
    _storage = None


# 기존 서비스 코드와의 호환 표면 (모듈 함수 → 싱글턴 위임)
def put_original(key: str, data: bytes | bytearray) -> None:
    get_storage().save_original(key, data)


def get_original(key: str) -> bytes:
    return get_storage().read_original(key)


def original_exists(key: str) -> bool:
    return get_storage().original_exists(key)


def delete_original(key: str) -> None:
    get_storage().delete_original(key)
