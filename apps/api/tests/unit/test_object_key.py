import uuid

from app.services.documents.storage import object_key


def test_key_format():
    doc_id = uuid.uuid4()
    assert object_key(doc_id) == f"documents/{doc_id}/original.pdf"


def test_key_never_contains_original_filename():
    """원본 파일명은 메타데이터로만 저장 — 키는 UUID로만 구성된다."""
    doc_id = uuid.uuid4()
    parts = object_key(doc_id).split("/")
    assert parts[0] == "documents"
    assert uuid.UUID(parts[1]) == doc_id
    assert parts[2] == "original.pdf"
