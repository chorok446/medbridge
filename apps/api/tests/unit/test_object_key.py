import uuid

from app.services.documents.storage import object_key


def test_key_format():
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    key = object_key(user_id, doc_id)
    assert key == f"users/{user_id}/documents/{doc_id}/original.pdf"


def test_key_never_contains_original_filename():
    """원본 파일명은 메타데이터로만 저장 — 키는 UUID로만 구성된다."""
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    key = object_key(user_id, doc_id)
    parts = key.split("/")
    assert parts[0] == "users"
    assert uuid.UUID(parts[1]) == user_id
    assert parts[2] == "documents"
    assert uuid.UUID(parts[3]) == doc_id
    assert parts[4] == "original.pdf"
