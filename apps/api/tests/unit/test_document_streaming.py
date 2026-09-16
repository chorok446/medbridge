"""대형 업로드가 전체 payload를 Python heap에 누적하지 않는지 검증한다."""

import asyncio

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.documents import service

MIB = 1024 * 1024


class RepeatingPdfUpload:
    """한 개의 1MiB buffer를 재사용하는 논리적 대형 upload stream."""

    filename = "large.pdf"

    def __init__(self, chunk_count: int) -> None:
        self._remaining = chunk_count
        self._chunk = b"%PDF-" + (b"x" * (MIB - 5))
        self.read_count = 0

    async def read(self, size: int) -> bytes:
        assert size == MIB
        if self._remaining == 0:
            return b""
        self._remaining -= 1
        self.read_count += 1
        return self._chunk


class CountingStagedUpload:
    """chunk를 보관하지 않고 write 크기만 세는 disk sink 대역."""

    def __init__(self) -> None:
        self.total = 0
        self.max_write = 0
        self.write_count = 0
        self.sealed = False

    def write(self, data: bytes) -> int:
        self.total += len(data)
        self.max_write = max(self.max_write, len(data))
        self.write_count += 1
        return len(data)

    def seal(self) -> None:
        self.sealed = True


async def test_301_mib_logical_upload_stays_chunk_bounded(monkeypatch):
    upload = RepeatingPdfUpload(chunk_count=301)
    staged = CountingStagedUpload()
    discarded: list[CountingStagedUpload] = []
    monkeypatch.setattr(service.storage, "create_staged_upload", lambda: staged)
    monkeypatch.setattr(service.storage, "discard_staged_upload", discarded.append)

    result = await service._stage_upload(upload, max_bytes=800 * MIB)

    assert result.file_size == 301 * MIB
    assert len(result.sha256) == 64
    assert staged.total == result.file_size
    assert staged.max_write == MIB
    assert staged.write_count == 301
    assert staged.sealed is True
    assert discarded == []


async def test_large_asgi_chunk_is_split_into_one_mib_writes(monkeypatch):
    staged = CountingStagedUpload()
    monkeypatch.setattr(service.storage, "create_staged_upload", lambda: staged)
    monkeypatch.setattr(service.storage, "discard_staged_upload", lambda _staged: None)

    async def chunks():
        yield b"%PDF-" + (b"x" * (3 * MIB))

    result = await service._stage_chunks(chunks(), max_bytes=4 * MIB)

    assert result.file_size == 3 * MIB + 5
    assert staged.max_write == MIB
    assert staged.write_count == 4


async def test_over_limit_stream_stops_and_discards_staging(monkeypatch):
    upload = RepeatingPdfUpload(chunk_count=4)
    staged = CountingStagedUpload()
    discarded: list[CountingStagedUpload] = []
    monkeypatch.setattr(service.storage, "create_staged_upload", lambda: staged)
    monkeypatch.setattr(service.storage, "discard_staged_upload", discarded.append)

    try:
        await service._stage_upload(upload, max_bytes=2 * MIB)
    except AppError as exc:
        assert exc.code == ErrorCode.FILE_TOO_LARGE
    else:
        raise AssertionError("oversized upload must fail")

    assert staged.total == 2 * MIB
    assert staged.max_write == MIB
    assert upload.read_count == 3  # 제한 초과 chunk에서 즉시 중단
    assert discarded == [staged]


async def test_cancelled_upload_discards_staging(monkeypatch):
    class CancelledUpload(RepeatingPdfUpload):
        async def read(self, size: int) -> bytes:
            if self.read_count == 1:
                raise asyncio.CancelledError
            return await super().read(size)

    upload = CancelledUpload(chunk_count=3)
    staged = CountingStagedUpload()
    discarded: list[CountingStagedUpload] = []
    monkeypatch.setattr(service.storage, "create_staged_upload", lambda: staged)
    monkeypatch.setattr(service.storage, "discard_staged_upload", discarded.append)

    with pytest.raises(asyncio.CancelledError):
        await service._stage_upload(upload, max_bytes=800 * MIB)

    assert staged.total == MIB
    assert discarded == [staged]
