"""파일 저장소 계층.

FileStorage 인터페이스 + 로컬 파일시스템 구현(LocalFileStorage).
PDF는 저장소·설치 디렉터리가 아닌 OS 앱 데이터 경로에 저장한다.
경로는 UUID로만 구성하고(원본 파일명 금지), 앱 데이터 밖 접근을 차단한다.
"""

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.errors import AppError, ErrorCode
from app.core.paths import get_path_provider


def object_key(document_id: uuid.UUID) -> str:
    """원본 파일명은 절대 키에 쓰지 않는다 (DB 메타데이터로만 보존)."""
    return f"documents/{document_id}/original.pdf"


class FileStorage(Protocol):
    def create_staged_upload(self) -> "StagedUpload": ...
    def promote_staged(self, key: str, staged: "StagedUpload") -> None: ...
    def discard_staged(self, staged: "StagedUpload") -> None: ...
    def cleanup_staged(self) -> int: ...
    def save_original(self, key: str, data: bytes | bytearray) -> None: ...
    def read_original(self, key: str) -> bytes: ...
    def original_exists(self, key: str) -> bool: ...
    def delete_original(self, key: str) -> None: ...
    def resolve_path(self, key: str) -> Path: ...


@dataclass
class StagedUpload:
    """앱 데이터 볼륨에 열린 업로드 임시 파일.

    원본 파일명은 경로에 포함하지 않는다. 호출자는 ``seal`` 뒤에 저장소를 통해
    promote/discard해야 하며, 두 작업은 여러 번 호출해도 안전하다.
    """

    path: Path
    _file: BinaryIO
    _closed: bool = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise RuntimeError("sealed staged upload cannot be written")
        try:
            written = self._file.write(data)
            if written != len(data):
                raise OSError("short write to staged upload")
            return written
        except OSError as exc:
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "파일 저장에 실패했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

    def seal(self) -> None:
        """파일 내용을 디스크에 동기화하고 쓰기 핸들을 닫는다."""
        if self._closed:
            return
        failure: OSError | None = None
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as exc:
            failure = exc
        finally:
            try:
                self._file.close()
            except OSError as exc:
                failure = failure or exc
            finally:
                self._closed = True
        if failure is not None:
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "파일 저장에 실패했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from failure

    def close(self) -> None:
        if not self._closed:
            try:
                self._file.close()
            finally:
                self._closed = True


class LocalFileStorage:
    """앱 데이터 디렉터리 하위에 원자적으로 저장하는 로컬 구현."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def staging_root(self) -> Path:
        return self._root / ".staging"

    def resolve_path(self, key: str) -> Path:
        """경로 순회 방지: 최종 경로가 반드시 앱 데이터 루트 안이어야 한다."""
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 파일 경로입니다.", status_code=400)
        return path

    def _resolve_staged_path(self, path: Path) -> Path:
        """우리가 만든 staging 파일만 승격·삭제할 수 있게 범위를 제한한다."""
        staging_root = self.staging_root.resolve()
        resolved = path.resolve()
        if (
            staging_root.parent != self._root
            or resolved.parent != staging_root
            or not resolved.is_relative_to(staging_root)
            or not resolved.name.startswith("upload-")
            or resolved.suffix != ".tmp"
        ):
            raise AppError(
                ErrorCode.VALIDATION_FAILED, "잘못된 임시 파일 경로입니다.", status_code=400
            )
        return resolved

    def create_staged_upload(self) -> StagedUpload:
        """최종 원본과 같은 앱 데이터 볼륨에 익명 staging 파일을 만든다."""
        try:
            self.staging_root.mkdir(parents=True, exist_ok=True)
            if self.staging_root.resolve().parent != self._root:
                raise AppError(
                    ErrorCode.VALIDATION_FAILED,
                    "잘못된 임시 파일 경로입니다.",
                    status_code=400,
                )
            fd, raw_path = tempfile.mkstemp(
                dir=self.staging_root,
                prefix="upload-",
                suffix=".tmp",
            )
            try:
                handle = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                Path(raw_path).unlink(missing_ok=True)
                raise
            return StagedUpload(path=Path(raw_path), _file=handle)
        except OSError as exc:
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "임시 파일을 만들 수 없습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

    def promote_staged(self, key: str, staged: StagedUpload) -> None:
        """동기화된 staging 파일을 UUID 원본 경로로 원자적으로 승격한다."""
        source = self._resolve_staged_path(staged.path)
        staged.seal()
        target = self.resolve_path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # source와 target은 같은 앱 데이터 루트 아래라 같은 볼륨의 atomic replace다.
            os.replace(source, target)
        except OSError as exc:
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED,
                "파일 저장에 실패했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.",
                status_code=502,
                retryable=True,
            ) from exc

    def discard_staged(self, staged: StagedUpload) -> None:
        """중복·검사 실패·취소 시 staging 파일을 남기지 않는다."""
        try:
            staged.close()
        finally:
            path = self._resolve_staged_path(staged.path)
            path.unlink(missing_ok=True)

    def cleanup_staged(self) -> int:
        """single-instance 획득 뒤 이전 강제 종료가 남긴 staging 조각만 회수한다."""
        root = self.staging_root
        if not root.is_dir() or root.is_symlink():
            return 0
        removed = 0
        for path in root.iterdir():
            # 외부 symlink/reparse target과 우리가 만들지 않은 파일은 건드리지 않는다.
            if (
                path.is_symlink()
                or not path.name.startswith("upload-")
                or path.suffix != ".tmp"
                or not path.is_file()
            ):
                continue
            path.unlink()
            removed += 1
        return removed

    def save_original(self, key: str, data: bytes | bytearray) -> None:
        """작은 내부 호출용 호환 API. 업로드 경로는 staging에 직접 스트리밍한다."""
        staged = self.create_staged_upload()
        try:
            staged.write(bytes(data))
            self.promote_staged(key, staged)
        finally:
            self.discard_staged(staged)

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
def create_staged_upload() -> StagedUpload:
    return get_storage().create_staged_upload()


def discard_staged_upload(staged: StagedUpload) -> None:
    get_storage().discard_staged(staged)


def cleanup_staged_uploads() -> int:
    return get_storage().cleanup_staged()


def put_original(key: str, data: bytes | bytearray | StagedUpload) -> None:
    """원본 저장 호환 표면.

    서비스 업로드는 ``StagedUpload``를 전달해 복사 없이 승격하고, 기존 소형 호출과
    테스트는 bytes를 계속 사용할 수 있다.
    """
    store = get_storage()
    if isinstance(data, StagedUpload):
        store.promote_staged(key, data)
    else:
        store.save_original(key, data)


def get_original(key: str) -> bytes:
    return get_storage().read_original(key)


def resolve_original_path(key: str) -> Path:
    return get_storage().resolve_path(key)


def original_exists(key: str) -> bool:
    return get_storage().original_exists(key)


def delete_original(key: str) -> None:
    get_storage().delete_original(key)
