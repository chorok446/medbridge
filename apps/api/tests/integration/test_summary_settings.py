"""요약 모델 설정 + keyring — API 키는 응답·DB에 노출되지 않는다."""

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.summary import SummarySettings
from app.services.summary import secrets


class TestSummarySettings:
    async def test_default_settings_disabled(self, client):
        res = await client.get("/api/settings/summary")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["enabled"] is False
        assert data["hasApiKey"] is False

    async def test_update_stores_key_in_keyring_not_db_or_response(self, client):
        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "https://api.example.com/v1",
                "modelName": "gpt-x",
                "isLocal": False,
                "apiKey": "super-secret-key-123",
            },
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["hasApiKey"] is True
        # 응답 어디에도 키 원문이 없어야 한다
        assert "super-secret-key-123" not in res.text
        # DB에도 키가 저장되지 않아야 한다
        async with get_session_factory()() as s:
            row = (await s.execute(select(SummarySettings))).scalars().first()
            assert row is not None
            dumped = str(row.__dict__)
            assert "super-secret-key-123" not in dumped
        # keyring에는 저장돼 있다
        assert secrets.get_api_key() == "super-secret-key-123"

    async def test_delete_key(self, client):
        await client.put(
            "/api/settings/summary",
            json={"enabled": True, "providerType": "openai_compatible", "apiKey": "k"},
        )
        assert secrets.has_api_key() is True
        res = await client.delete("/api/settings/summary/key")
        assert res.status_code == 200
        assert res.json()["data"]["hasApiKey"] is False
        assert secrets.has_api_key() is False

    async def test_invalid_provider_type_422(self, client):
        res = await client.put(
            "/api/settings/summary", json={"providerType": "telepathy"}
        )
        assert res.status_code == 422

    async def test_connection_test_disabled(self, client):
        res = await client.post("/api/settings/summary/test")
        assert res.status_code == 200
        assert res.json()["data"]["ok"] is False

    async def test_connection_test_deterministic_ok(self, client):
        await client.put(
            "/api/settings/summary",
            json={"enabled": True, "providerType": "deterministic"},
        )
        res = await client.post("/api/settings/summary/test")
        assert res.status_code == 200
        assert res.json()["data"]["ok"] is True
