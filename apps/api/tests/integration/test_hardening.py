"""Codex 리뷰 지적 사항 회귀 테스트 (보상·복구·인증 경계)."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_session_factory
from app.models.document import Document
from app.models.enums import ProcessingStatus
from app.services.documents import storage
from tests.conftest import make_pdf
from tests.integration.conftest import drain_jobs


def upload_kwargs(data: bytes, filename: str = "test.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


class TestDeleteRetry:
    async def test_failed_delete_can_be_retried(self, client, monkeypatch):
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        doc_id = res.json()["data"]["id"]
        await drain_jobs()

        calls = {"n": 0}
        real_delete = storage.delete_original

        def flaky_delete(key: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk error")
            real_delete(key)

        from app.services.documents import service

        monkeypatch.setattr(service.storage, "delete_original", flaky_delete)

        first = await client.delete(f"/api/documents/{doc_id}")
        assert first.status_code == 502
        assert first.json()["error"]["retryable"] is True

        # deleting 상태에서 재시도가 가능해야 한다 (idempotent)
        second = await client.delete(f"/api/documents/{doc_id}")
        assert second.status_code == 200
        assert second.json()["data"]["processingStatus"] == "deleted"


class TestCrashBoundary:
    async def test_crashed_validation_does_not_stick_in_validating(self, client, monkeypatch):
        from app.services.tasks import validate as validate_mod

        def boom(_key: str) -> bytes:
            raise RuntimeError("unexpected crash")

        monkeypatch.setattr(validate_mod.storage, "get_original", boom)
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        doc_id = res.json()["data"]["id"]
        await drain_jobs()
        monkeypatch.undo()

        detail = (await client.get(f"/api/documents/{doc_id}")).json()["data"]
        assert detail["processingStatus"] == "failed"  # validating 고착 금지
        assert detail["failureMessage"]
        # 실패 후 재시도하면 정상 완료돼야 한다
        await client.post(f"/api/documents/{doc_id}/retry")
        await drain_jobs()
        after = (await client.get(f"/api/documents/{doc_id}")).json()["data"]
        assert after["processingStatus"] == "extracted"


class TestStartupRecovery:
    async def _set_status(self, doc_id: str, status: ProcessingStatus) -> None:
        async with get_session_factory()() as session:
            doc = await session.get(Document, uuid.UUID(doc_id))
            assert doc is not None
            doc.processing_status = status
            await session.commit()

    async def test_uploaded_state_is_requeued(self, client):
        res = await client.post("/api/documents", **upload_kwargs(make_pdf()))
        doc_id = res.json()["data"]["id"]
        await drain_jobs()
        await self._set_status(doc_id, ProcessingStatus.UPLOADED)

        from app.services.tasks.runner import get_task_runner

        await get_task_runner().recover_interrupted()
        await drain_jobs()
        detail = (await client.get(f"/api/documents/{doc_id}")).json()["data"]
        assert detail["processingStatus"] == "extracted"

    async def test_uploading_state_is_failed_with_guidance(self, client):
        res = await client.post("/api/documents", **upload_kwargs(make_pdf(pages=2)))
        doc_id = res.json()["data"]["id"]
        await drain_jobs()
        await self._set_status(doc_id, ProcessingStatus.UPLOADING)

        from app.services.tasks.runner import get_task_runner

        await get_task_runner().recover_interrupted()
        detail = (await client.get(f"/api/documents/{doc_id}")).json()["data"]
        assert detail["processingStatus"] == "failed"
        assert "다시 업로드" in detail["failureMessage"]


class TestDuplicateAfterFailure:
    async def test_failed_doc_without_file_does_not_block_reupload(self, client, monkeypatch):
        from app.core.errors import AppError, ErrorCode
        from app.services.documents import service

        data = make_pdf(pages=3)

        def boom(key: str, payload: bytes) -> None:
            raise AppError(ErrorCode.STORAGE_UPLOAD_FAILED, "fail", status_code=502, retryable=True)

        monkeypatch.setattr(service.storage, "put_original", boom)
        first = await client.post("/api/documents", **upload_kwargs(data))
        assert first.status_code == 502
        monkeypatch.undo()

        # 같은 파일 재업로드가 duplicate로 막히지 않아야 한다
        second = await client.post("/api/documents", **upload_kwargs(data))
        assert second.status_code == 201
        assert second.json()["data"]["duplicate"] is False
        await drain_jobs()
        detail = (await client.get(f"/api/documents/{second.json()['data']['id']}")).json()[
            "data"
        ]
        assert detail["processingStatus"] == "extracted"


class TestTokenGuard:
    @pytest.fixture
    async def token_client(self, monkeypatch):
        from app.core import config

        monkeypatch.setenv("MEDBRIDGE_API_TOKEN", "test-token-123")
        config.get_settings.cache_clear()
        from app.main import create_app

        app2 = create_app()
        transport = ASGITransport(app=app2)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        config.get_settings.cache_clear()

    async def test_requests_without_token_rejected(self, token_client):
        res = await token_client.get("/api/profile")
        assert res.status_code == 401

    async def test_header_token_accepted(self, token_client):
        res = await token_client.get(
            "/api/profile", headers={"X-MedBridge-Token": "test-token-123"}
        )
        assert res.status_code == 200

    async def test_query_token_only_valid_for_file_endpoint(self, token_client):
        # 일반 경로에서는 query 토큰을 받지 않는다
        res = await token_client.get("/api/documents?token=test-token-123")
        assert res.status_code == 401

    async def test_health_open_for_shell_monitoring(self, token_client):
        assert (await token_client.get("/health")).status_code == 200


class TestProductionFailFast:
    def test_production_without_token_refuses_startup(self):
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(app_env="production", medbridge_api_token=None)

    def test_production_with_token_ok(self):
        from app.core.config import Settings

        s = Settings(app_env="production", medbridge_api_token="tok")
        assert s.app_env == "production"
