from fastapi.testclient import TestClient

from app.core.errors import AppError, ErrorCode
from app.main import app


def test_app_error_becomes_error_envelope():
    @app.get("/__test/app-error")
    async def _raise() -> None:
        raise AppError(
            ErrorCode.ENCRYPTED_PDF, "암호화된 PDF입니다.", status_code=400, retryable=False
        )

    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/app-error")
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["code"] == "ENCRYPTED_PDF"
    assert body["error"]["retryable"] is False
    assert body["meta"]["correlationId"]
    assert res.headers["X-Correlation-ID"] == body["meta"]["correlationId"]


def test_internal_error_hides_details():
    """스택 트레이스·내부 메시지가 응답에 노출되지 않는다."""

    @app.get("/__test/boom")
    async def _boom() -> None:
        raise RuntimeError("secret internal detail s3://bucket/key")

    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/boom")
    assert res.status_code == 500
    body = res.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in res.text
    assert body["error"]["retryable"] is True


def test_correlation_id_passthrough():
    client = TestClient(app)
    res = client.get("/health", headers={"X-Correlation-ID": "test-cid-123"})
    assert res.headers["X-Correlation-ID"] == "test-cid-123"
