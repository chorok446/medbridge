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

    async def test_deterministic_rejected_by_public_api(self, client):
        # deterministic은 테스트 전용 — 공개 설정 API로는 선택할 수 없다
        res = await client.put(
            "/api/settings/summary", json={"providerType": "deterministic"}
        )
        assert res.status_code == 422

    async def test_connection_test_deterministic_ok(self, client):
        # deterministic은 공개 API로 못 켜므로 행을 직접 넣어 확인한다(테스트 경로)
        async with get_session_factory()() as s:
            s.add(SummarySettings(enabled=True, provider_type="deterministic"))
            await s.commit()
        res = await client.post("/api/settings/summary/test")
        assert res.status_code == 200
        assert res.json()["data"]["ok"] is True


class TestEndpointValidationViaApi:
    async def test_external_http_endpoint_rejected(self, client):
        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "http://api.example.com/v1",  # 외부인데 HTTP → 거부
                "modelName": "m",
                "isLocal": False,
            },
        )
        assert res.status_code == 422

    async def test_external_endpoint_with_userinfo_rejected(self, client):
        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "https://a@evil.example/v1",
                "isLocal": False,
            },
        )
        assert res.status_code == 422

    async def test_local_endpoint_non_loopback_rejected(self, client):
        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "http://192.168.0.10:1234/v1",  # 로컬인데 loopback 아님
                "isLocal": True,
            },
        )
        assert res.status_code == 422

    async def test_valid_local_loopback_accepted(self, client):
        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "http://127.0.0.1:11434/v1/",
                "modelName": "llama",
                "isLocal": True,
            },
        )
        assert res.status_code == 200
        # 정규화: trailing slash 제거
        assert res.json()["data"]["endpoint"] == "http://127.0.0.1:11434/v1"


class TestSecretHandling:
    SECRET = "MEDBRIDGE_TEST_SECRET_DO_NOT_LEAK"

    async def test_secret_never_leaks(self, client, monkeypatch):
        # 네트워크 없이 인증 실패를 모사
        from app.services.summary import endpoint as endpoint_mod

        def _fail(*a, **k):
            raise endpoint_mod.SummaryNetworkError("auth_failed")

        monkeypatch.setattr(endpoint_mod, "post_json", _fail)

        put = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "https://api.example.com/v1",
                "modelName": "gpt-x",
                "isLocal": False,
                "apiKey": self.SECRET,
            },
        )
        assert put.status_code == 200
        assert self.SECRET not in put.text

        # 설정 조회·연결 확인·오류 보고서 어디에도 키가 없어야 한다
        get = await client.get("/api/settings/summary")
        assert self.SECRET not in get.text
        test = await client.post("/api/settings/summary/test")
        assert self.SECRET not in test.text
        assert test.json()["data"]["ok"] is False
        report = await client.get("/api/system/error-report")
        assert self.SECRET not in report.text

        # DB 어디에도 키가 없다
        async with get_session_factory()() as s:
            row = (await s.execute(select(SummarySettings))).scalars().first()
            assert self.SECRET not in str(row.__dict__)


class TestSettingsAtomicity:
    async def test_keyring_failure_rolls_back_db(self, client, monkeypatch):
        from app.services.summary import secrets as secrets_mod

        def _boom(_value):
            raise RuntimeError("keyring unavailable")

        monkeypatch.setattr(secrets_mod, "set_api_key", _boom)

        res = await client.put(
            "/api/settings/summary",
            json={
                "enabled": True,
                "providerType": "openai_compatible",
                "endpoint": "https://api.example.com/v1",
                "modelName": "m",
                "isLocal": False,
                "apiKey": "will-fail",
            },
        )
        assert res.status_code == 500
        # DB에 설정이 커밋되지 않아야 한다(부분 저장 방지)
        get = await client.get("/api/settings/summary")
        assert get.json()["data"]["enabled"] is False
