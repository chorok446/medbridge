import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.paths import get_path_provider
from app.db.session import get_session_factory
from app.models.document import Document
from app.models.enums import ProcessingStatus
from app.models.user import User
from app.services.documents import storage
from tests.conftest import make_encrypted_pdf, make_pdf
from tests.integration.conftest import drain_jobs

REPO_ROOT = Path(__file__).resolve().parents[4]


def upload_kwargs(data: bytes, filename: str = "test.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_and_wait(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    doc = res.json()["data"]
    await drain_jobs()
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

        await drain_jobs()

        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "ready"
        assert detail["pageCount"] == 2
        assert detail["processingProgress"] == 100

        jobs = (await client.get(f"/api/documents/{created['id']}/jobs")).json()["data"]
        assert len(jobs) == 1
        assert jobs[0]["status"] == "succeeded"

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

    async def test_file_stored_in_app_data_with_uuid_key(self, client):
        created = (
            await client.post("/api/documents", **upload_kwargs(make_pdf(), "환자차트_홍길동.pdf"))
        ).json()["data"]
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(created["id"]))
            assert doc is not None and doc.storage_key is not None
            # 경로에 원본 파일명이 없다
            assert "환자차트" not in doc.storage_key
            assert doc.storage_key == f"documents/{doc.id}/original.pdf"
            assert doc.original_filename == "환자차트_홍길동.pdf"
            # 실제 파일이 앱 데이터 디렉터리 안에 있다
            path = storage.get_storage().resolve_path(doc.storage_key)
            assert path.is_file()
            assert path.is_relative_to(get_path_provider().root.resolve())
            assert not path.is_relative_to(REPO_ROOT)

    async def test_upload_leaves_git_repo_untouched(self, client):
        """업로드·삭제가 저장소 파일에 아무 영향을 주지 않는다."""
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        detail = await upload_and_wait(client, make_pdf())
        await client.delete(f"/api/documents/{detail['id']}")
        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        assert before == after


class TestWorkerValidation:
    async def test_encrypted_pdf_fails(self, client):
        detail = await upload_and_wait(client, make_encrypted_pdf())
        assert detail["processingStatus"] == "failed"
        assert detail["failureCode"] == "ENCRYPTED_PDF"
        assert detail["failureMessage"]

    async def test_validation_is_idempotent(self, client):
        from app.services.tasks.validate import validate_document

        created = (await client.post("/api/documents", **upload_kwargs(make_pdf()))).json()["data"]
        await drain_jobs()
        await validate_document(uuid.UUID(created["id"]), "cid-rerun")  # 중복 실행 무해
        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "ready"

    async def test_retry_after_failure(self, client):
        detail = await upload_and_wait(client, make_encrypted_pdf())
        res = await client.post(f"/api/documents/{detail['id']}/retry")
        assert res.status_code == 200
        assert res.json()["data"]["processingStatus"] == "queued"
        await drain_jobs()
        after = (await client.get(f"/api/documents/{detail['id']}")).json()["data"]
        assert after["processingStatus"] == "failed"  # 같은 파일이므로 다시 실패
        jobs = (await client.get(f"/api/documents/{detail['id']}/jobs")).json()["data"]
        assert len(jobs) == 2

    async def test_retry_rejected_when_not_failed(self, client):
        detail = await upload_and_wait(client, make_pdf())
        res = await client.post(f"/api/documents/{detail['id']}/retry")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "INVALID_STATE"

    async def test_interrupted_job_recovery(self, client):
        """앱 재시작 시 validating에 멈춘 문서가 복구된다."""
        from app.services.tasks.runner import get_task_runner

        created = (await client.post("/api/documents", **upload_kwargs(make_pdf(pages=3)))).json()[
            "data"
        ]
        await drain_jobs()
        # 앱 종료 중 중단된 상태를 재현
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(created["id"]))
            assert doc is not None
            doc.processing_status = ProcessingStatus.VALIDATING
            doc.page_count = None
            await session.commit()

        recovered = await get_task_runner().recover_interrupted()
        assert recovered == 1
        await drain_jobs()
        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "ready"
        assert detail["pageCount"] == 3


class TestOwnership:
    async def make_other_users_document(self) -> uuid.UUID:
        async with get_session_factory()() as session:
            other = User(display_name="다른 프로필")
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

    async def test_cannot_read_other_profiles_document(self, client):
        await client.get("/api/profile")  # 로컬 프로필(1행)을 먼저 생성
        doc_id = await self.make_other_users_document()
        res = await client.get(f"/api/documents/{doc_id}")
        assert res.status_code == 404  # 존재 여부도 노출하지 않는다

    async def test_cannot_delete_other_profiles_document(self, client):
        await client.get("/api/profile")
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
            assert doc is not None
            assert str(doc.user_id) != fake_user_id


class TestDeleteAndRename:
    async def test_delete_removes_file_and_soft_deletes(self, client):
        detail = await upload_and_wait(client, make_pdf())
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
        assert (await client.get(f"/api/documents/{detail['id']}")).status_code == 404

    async def test_rename(self, client):
        detail = await upload_and_wait(client, make_pdf())
        res = await client.patch(f"/api/documents/{detail['id']}", json={"title": "새 제목"})
        assert res.status_code == 200
        assert res.json()["data"]["title"] == "새 제목"

    async def test_rename_rejects_blank(self, client):
        detail = await upload_and_wait(client, make_pdf())
        res = await client.patch(f"/api/documents/{detail['id']}", json={"title": "   "})
        assert res.status_code == 422


class TestCompensation:
    async def test_storage_failure_marks_failed(self, client, monkeypatch):
        from app.core.errors import AppError, ErrorCode
        from app.services.documents import service

        def boom(key: str, data: bytes) -> None:
            raise AppError(
                ErrorCode.STORAGE_UPLOAD_FAILED, "fail", status_code=502, retryable=True
            )

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

        def raise_error(*_args):
            raise RuntimeError("runner down")

        monkeypatch.setattr(service, "_enqueue_validate", raise_error)
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        assert res.status_code == 201  # 업로드 자체는 성공, 상태로 실패를 알린다
        created = res.json()["data"]
        detail = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert detail["processingStatus"] == "failed"
        assert detail["failureCode"] == "QUEUE_ENQUEUE_FAILED"

        monkeypatch.undo()
        res = await client.post(f"/api/documents/{created['id']}/retry")
        assert res.status_code == 200
        await drain_jobs()
        after = (await client.get(f"/api/documents/{created['id']}")).json()["data"]
        assert after["processingStatus"] == "ready"


class TestPersistence:
    async def test_data_survives_engine_restart(self, client):
        """앱 재실행(엔진·세션 재생성) 후에도 문서와 파일이 유지된다."""
        from app.db.session import reset_engine_cache
        from app.services.documents.storage import reset_storage_cache

        detail = await upload_and_wait(client, make_pdf())
        assert detail["processingStatus"] == "ready"

        reset_engine_cache()
        reset_storage_cache()

        listing = (await client.get("/api/documents")).json()["data"]
        assert any(d["id"] == detail["id"] for d in listing["items"])
        file_res = await client.get(f"/api/documents/{detail['id']}/file")
        assert file_res.status_code == 200
        assert file_res.headers["content-type"] == "application/pdf"


class TestListingAndFile:
    async def test_list_pagination(self, client):
        for i in range(3):
            await client.post("/api/documents", **upload_kwargs(make_pdf(pages=1 + i)))
        await drain_jobs()
        page1 = (await client.get("/api/documents?limit=2")).json()["data"]
        assert len(page1["items"]) == 2
        assert page1["nextCursor"]
        page2 = (
            await client.get(f"/api/documents?limit=2&cursor={page1['nextCursor']}")
        ).json()["data"]
        assert len(page2["items"]) == 1
        assert page2["nextCursor"] is None

    async def test_file_endpoint_serves_pdf(self, client):
        data = make_pdf(pages=2)
        detail = await upload_and_wait(client, data)
        res = await client.get(f"/api/documents/{detail['id']}/file")
        assert res.status_code == 200
        assert res.content == data
