"""LocalFileStorage — 원자적 저장·경로 순회 방지·정리."""

import uuid
from dataclasses import replace
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

    def test_staged_upload_is_promoted_without_leftover(
        self, store: LocalFileStorage, tmp_path: Path
    ):
        key = object_key(uuid.uuid4())
        staged = store.create_staged_upload()
        assert staged.path.parent.resolve() == (tmp_path / ".staging").resolve()
        assert "original" not in staged.path.name

        staged.write(b"%PDF-staged")
        store.promote_staged(key, staged)

        assert store.read_original(key) == b"%PDF-staged"
        assert not staged.path.exists()

    def test_duplicate_or_failed_upload_can_discard_staging(self, store: LocalFileStorage):
        staged = store.create_staged_upload()
        staged.write(b"%PDF-temporary")
        path = staged.path

        store.discard_staged(staged)
        store.discard_staged(staged)  # 보상 처리는 idempotent해야 한다

        assert not path.exists()

    def test_forged_staging_path_is_rejected(self, store: LocalFileStorage, tmp_path: Path):
        staged = store.create_staged_upload()
        forged = replace(staged, path=tmp_path / "outside.tmp")
        try:
            with pytest.raises(AppError):
                store.promote_staged(object_key(uuid.uuid4()), forged)
        finally:
            store.discard_staged(staged)

    def test_startup_cleanup_removes_only_owned_staging_files(
        self, store: LocalFileStorage, tmp_path: Path
    ):
        store.staging_root.mkdir(parents=True, exist_ok=True)
        stale = store.staging_root / "upload-stale.tmp"
        unrelated = store.staging_root / "keep-me.txt"
        stale.write_bytes(b"partial-pdf")
        unrelated.write_bytes(b"diagnostic")

        assert store.cleanup_staged() == 1
        assert not stale.exists()
        assert unrelated.read_bytes() == b"diagnostic"

    def test_startup_cleanup_does_not_follow_staging_symlink(
        self, store: LocalFileStorage, tmp_path: Path
    ):
        store.staging_root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside-private.pdf"
        outside.write_bytes(b"must survive")
        link = store.staging_root / "upload-forged.tmp"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("현재 Windows 계정은 symlink 생성 권한이 없습니다")

        assert store.cleanup_staged() == 0
        assert outside.read_bytes() == b"must survive"
        assert link.is_symlink()


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
