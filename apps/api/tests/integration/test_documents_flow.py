import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.document import Document
from app.models.enums import ProcessingStatus
from app.models.user import User
from app.services.documents import storage
from app.workers.tasks import validate_file
from tests.conftest import make_encrypted_pdf, make_pdf

REPO_ROOT = Path(__file__).resolve().parents[4]


def upload_kwargs(data: bytes, filename: str = "test.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_and_validate(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    doc = res.json()["data"]
    validate_file(doc["id"], "test-cid")  # worker 인라인 실행
    detail = await client.get(f"/api/documents/{doc['id']}")
    return detail.json()["data"]


class TestUploadFlow:
    async def test_normal_upload_to_ready(self, client):
        data = make_pdf(pages=2)
        res = await client.post("/api/documents", **upload_kwargs(data))
        assert res.status_code == 201
        created = res.json()["data"]
        assert created["processingStatus"] == "queued"
        assert created["duplicate"] is False

        validate_file(created["id"], "test-cid")

        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "ready"
        assert detail["pageCount"] == 2
        assert detail["processingProgress"] == 100

        jobs = (await client.get(f"/api/documents/{created['id']}/jobs")).json()["data"]
        assert len(jobs) == 1
        assert jobs[0]["status"] == "succeeded"
        assert jobs[0]["correlationId"]

    async def test_non_pdf_rejected(self, client):
        res = await client.post(
            "/api/documents", files={"file": ("fake.pdf", b"MZ not a pdf", "application/pdf")}
        )
        assert res.status_code == 415
        assert res.json()["error"]["code"] == "INVALID_FILE_TYPE"

    async def test_empty_file_rejected(self, client):
        res = await client.post(
            "/api/documents", files={"file": ("empty.pdf", b"", "application/pdf")}
        )
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "EMPTY_PDF"

    async def test_duplicate_returns_existing(self, client):
        data = make_pdf()
        first = (await client.post("/api/documents", **upload_kwargs(data))).json()["data"]
        second = (await client.post("/api/documents", **upload_kwargs(data))).json()["data"]
        assert second["duplicate"] is True
        assert second["id"] == first["id"]

    async def test_object_key_is_uuid_based(self, client):
        data = make_pdf()
        created = (
            await client.post("/api/documents", **upload_kwargs(data, "환자차트_홍길동.pdf"))
        ).json()["data"]
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(created["id"]))
            assert doc is not None and doc.storage_key is not None
            assert "환자차트" not in doc.storage_key
            assert "홍길동" not in doc.storage_key
            assert doc.storage_key.startswith("users/")
            assert doc.original_filename == "환자차트_홍길동.pdf"

    async def test_upload_leaves_git_repo_untouched(self, client):
        """업로드·삭제가 저장소 파일에 아무 영향을 주지 않는다 (named volume 영속성)."""
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        detail = await upload_and_validate(client, make_pdf())
        await client.delete(f"/api/documents/{detail['id']}")
        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        assert before == after


class TestWorkerValidation:
    async def test_encrypted_pdf_fails_in_worker(self, client):
        detail = await upload_and_validate(client, make_encrypted_pdf())
        assert detail["processingStatus"] == "failed"
        assert detail["failureCode"] == "ENCRYPTED_PDF"
        assert detail["failureMessage"]

    async def test_worker_is_idempotent(self, client):
        created = (await client.post("/api/documents", **upload_kwargs(make_pdf()))).json()["data"]
        validate_file(created["id"], "cid-1")
        validate_file(created["id"], "cid-1")  # 중복 실행 — 상태 훼손 없어야 함
        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "ready"

    async def test_retry_after_failure(self, client):
        detail = await upload_and_validate(client, make_encrypted_pdf())
        res = await client.post(f"/api/documents/{detail['id']}/retry")
        assert res.status_code == 200
        assert res.json()["data"]["processingStatus"] == "queued"
        validate_file(detail["id"], "cid-retry")
        after = (await client.get(f"/api/documents/{detail['id']}")).json()["data"]
        assert after["processingStatus"] == "failed"  # 같은 파일이므로 다시 실패
        jobs = (await client.get(f"/api/documents/{detail['id']}/jobs")).json()["data"]
        assert len(jobs) == 2

    async def test_retry_rejected_when_not_failed(self, client):
        detail = await upload_and_validate(client, make_pdf())
        res = await client.post(f"/api/documents/{detail['id']}/retry")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "INVALID_STATE"


class TestOwnership:
    async def make_other_users_document(self) -> uuid.UUID:
        async with get_session_factory()() as session:
            other = User(
                email=f"other-{uuid.uuid4().hex[:8]}@example.com",
                password_hash="!",
                display_name="다른 사용자",
            )
            session.add(other)
            await session.flush()
            doc = Document(
                user_id=other.id,
                title="타인 문서",
                original_filename="other.pdf",
                sha256="0" * 64,
                file_size=10,
                processing_status=ProcessingStatus.READY,
            )
            session.add(doc)
            await session.commit()
            return doc.id

    async def test_cannot_read_other_users_document(self, client):
        doc_id = await self.make_other_users_document()
        res = await client.get(f"/api/documents/{doc_id}")
        assert res.status_code == 404  # 존재 여부도 노출하지 않는다

    async def test_cannot_delete_other_users_document(self, client):
        doc_id = await self.make_other_users_document()
        res = await client.delete(f"/api/documents/{doc_id}")
        assert res.status_code == 404
        async with get_session_factory()() as session:
            assert await session.get(Document, doc_id) is not None

    async def test_user_id_in_request_is_ignored(self, client):
        """body·query의 user_id로 소유권을 바꿀 수 없다."""
        fake_user_id = str(uuid.uuid4())
        res = await client.post(
            f"/api/documents?user_id={fake_user_id}",
            files={"file": ("t.pdf", make_pdf(), "application/pdf")},
            data={"user_id": fake_user_id},
        )
        assert res.status_code == 201
        created = res.json()["data"]
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(created["id"]))
            local_user = (
                await session.execute(select(User).where(User.email == "local-test@example.com"))
            ).scalar_one()
            assert doc is not None
            assert str(doc.user_id) == str(local_user.id)
            assert str(doc.user_id) != fake_user_id


class TestDelete:
    async def test_delete_removes_object_and_soft_deletes(self, client):
        detail = await upload_and_validate(client, make_pdf())
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(detail["id"]))
            assert doc is not None and doc.storage_key is not None
            key = doc.storage_key

        res = await client.delete(f"/api/documents/{detail['id']}")
        assert res.status_code == 200
        assert res.json()["data"]["processingStatus"] == "deleted"
        assert not storage.original_exists(key)

        listing = (await client.get("/api/documents")).json()["data"]
        assert all(d["id"] != detail["id"] for d in listing["items"])

        res = await client.get(f"/api/documents/{detail['id']}")
        assert res.status_code == 404


class TestCompensation:
    async def test_storage_failure_marks_failed(self, client, monkeypatch):
        from app.core.errors import AppError, ErrorCode
        from app.services.documents import service

        def boom(key: str, data: bytes) -> None:
            raise AppError(ErrorCode.STORAGE_UPLOAD_FAILED, "fail", status_code=502, retryable=True)

        monkeypatch.setattr(service.storage, "put_original", boom)
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        assert res.status_code == 502
        assert res.json()["error"]["code"] == "STORAGE_UPLOAD_FAILED"
        async with get_session_factory()() as session:
            doc = (await session.execute(select(Document))).scalars().first()
            assert doc is not None
            assert doc.processing_status == ProcessingStatus.FAILED
            assert doc.storage_key is None

    async def test_enqueue_failure_marks_failed_retryable(self, client, monkeypatch):
        from app.services.documents import service

        monkeypatch.setattr(
            service,
            "_enqueue_validate",
            lambda *a: (_ for _ in ()).throw(RuntimeError("redis down")),
        )
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        assert res.status_code == 201  # 업로드 자체는 성공, 상태로 실패를 알린다
        created = res.json()["data"]
        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "failed"
        assert detail["failureCode"] == "QUEUE_ENQUEUE_FAILED"

        # storage_key가 남아 있으므로 재시도 가능해야 한다
        monkeypatch.undo()
        res = await client.post(f"/api/documents/{created['id']}/retry")
        assert res.status_code == 200
        validate_file(created["id"], "cid")
        after = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert after["processingStatus"] == "ready"


class TestListing:
    async def test_list_pagination(self, client):
        for _ in range(3):
            await client.post("/api/documents", **upload_kwargs(make_pdf(pages=1 + _)))
        page1 = (await client.get("/api/documents?limit=2")).json()["data"]
        assert len(page1["items"]) == 2
        assert page1["nextCursor"]
        page2 = (await client.get(f"/api/documents?limit=2&cursor={page1['nextCursor']}")).json()[
            "data"
        ]
        assert len(page2["items"]) == 1
        assert page2["nextCursor"] is None

    async def test_download_url_presigned(self, client):
        detail = await upload_and_validate(client, make_pdf())
        res = await client.get(f"/api/documents/{detail['id']}/download-url")
        assert res.status_code == 200
        data = res.json()["data"]
        assert get_settings().minio_public_endpoint in data["url"]
        assert "X-Amz-Signature" in data["url"]
