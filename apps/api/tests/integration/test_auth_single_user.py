from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.user import User


class TestLocalSingleUser:
    async def test_me_returns_configured_user(self, client):
        res = await client.get("/api/auth/me")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["email"] == "local-test@example.com"
        assert data["displayName"] == "테스트 사용자"

    async def test_user_is_get_or_create_idempotent(self, client):
        await client.get("/api/auth/me")
        await client.get("/api/auth/me")
        async with get_session_factory()() as session:
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        assert count == 1

    async def test_register_disabled_in_single_user_mode(self, client):
        res = await client.post(
            "/api/auth/register",
            json={"email": "a@b.com", "password": "password123", "displayName": "x"},
        )
        assert res.status_code == 404

    async def test_login_disabled_in_single_user_mode(self, client):
        res = await client.post(
            "/api/auth/login", json={"email": "a@b.com", "password": "password123"}
        )
        assert res.status_code == 404
