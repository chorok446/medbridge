import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.services.documents.service import _decode_cursor, _encode_cursor


def test_roundtrip():
    ts = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    doc_id = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(ts, doc_id)) == (ts, doc_id)


def test_invalid_cursor_rejected():
    with pytest.raises(AppError):
        _decode_cursor("not-a-cursor!!")
