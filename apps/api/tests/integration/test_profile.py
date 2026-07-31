from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.user import User


class TestLocalProfile:
    async def test_profile_created_with_defaults(self, client):
        res = await client.get("/api/profile")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["displayName"] == "MedBridge 사용자"
        assert data["studyLevel"] == 1
        # 계정 개념(이메일·ID)을 노출하지 않는다
        assert "email" not in data
        assert "id" not in data

    async def test_profile_is_single_row(self, client):
        await client.get("/api/profile")
        await client.get("/api/profile")
        async with get_session_factory()() as session:
            count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        assert count == 1

    async def test_profile_update(self, client):
        res = await client.patch(
            "/api/profile", json={"displayName": "김간호", "studyLevel": 2}
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["displayName"] == "김간호"
        assert data["studyLevel"] == 2

    async def test_login_endpoints_do_not_exist(self, client):
        assert (await client.post("/api/auth/login", json={})).status_code == 404
        assert (await client.post("/api/auth/register", json={})).status_code == 404
