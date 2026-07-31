"""LocalFileStorage — 원자적 저장·경로 순회 방지·정리."""

import uuid
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services.documents.storage import LocalFileStorage, object_key


@pytest.fixture
def store(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path)


class TestSaveAndRead:
    def test_roundtrip(self, store: LocalFileStorage):
        key = object_key(uuid.uuid4())
        store.save_original(key, b"%PDF-data")
        assert store.original_exists(key)
        assert store.read_original(key) == b"%PDF-data"

    def test_no_tmp_files_left_after_save(self, store: LocalFileStorage, tmp_path: Path):
        key = object_key(uuid.uuid4())
        store.save_original(key, b"data")
        leftovers = list(tmp_path.rglob("*.tmp"))
        assert leftovers == []

    def test_overwrite_is_atomic_replace(self, store: LocalFileStorage):
        key = object_key(uuid.uuid4())
        store.save_original(key, b"v1")
        store.save_original(key, b"v2")
        assert store.read_original(key) == b"v2"


class TestPathTraversal:
    @pytest.mark.parametrize(
        "bad_key",
        [
            "../outside.pdf",
            "documents/../../etc/passwd",
            "documents/../../../secret",
        ],
    )
    def test_traversal_rejected(self, store: LocalFileStorage, bad_key: str):
        with pytest.raises(AppError):
            store.resolve_path(bad_key)

    def test_all_files_stay_under_root(self, store: LocalFileStorage, tmp_path: Path):
        key = object_key(uuid.uuid4())
        store.save_original(key, b"data")
        assert store.resolve_path(key).is_relative_to(tmp_path.resolve())


class TestDelete:
    def test_delete_removes_file_and_empty_dir(self, store: LocalFileStorage, tmp_path: Path):
        doc_id = uuid.uuid4()
        key = object_key(doc_id)
        store.save_original(key, b"data")
        store.delete_original(key)
        assert not store.original_exists(key)
        assert not (tmp_path / "documents" / str(doc_id)).exists()

    def test_delete_missing_is_noop(self, store: LocalFileStorage):
        store.delete_original(object_key(uuid.uuid4()))  # 예외 없이 통과
