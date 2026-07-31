"""앱 설정 — 요약 모델. API 키는 응답으로 반환하지 않는다(설정됨/미설정만)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.services.summary import settings_service
from app.utils.responses import wrap

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SummaryModelSettingsOut(CamelModel):
    enabled: bool
    provider_type: str
    endpoint: str | None
    model_name: str | None
    is_local: bool
    has_api_key: bool


class SummaryModelSettingsUpdate(CamelModel):
    enabled: bool | None = None
    provider_type: str | None = None
    endpoint: str | None = None
    model_name: str | None = None
    is_local: bool | None = None
    api_key: str | None = None  # keyring에만 저장, 응답으로 되돌리지 않는다


class ConnectionTestOut(CamelModel):
    ok: bool
    message: str


def _view_out(view) -> SummaryModelSettingsOut:
    return SummaryModelSettingsOut(
        enabled=view.enabled,
        provider_type=view.provider_type,
        endpoint=view.endpoint,
        model_name=view.model_name,
        is_local=view.is_local,
        has_api_key=view.has_api_key,
    )


@router.get("/summary", response_model=Envelope[SummaryModelSettingsOut])
async def get_summary_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return wrap(_view_out(await settings_service.get_settings_view(db)))


@router.put("/summary", response_model=Envelope[SummaryModelSettingsOut])
async def update_summary_settings(
    body: SummaryModelSettingsUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    view = await settings_service.update_settings(
        db,
        enabled=body.enabled,
        provider_type=body.provider_type,
        endpoint=body.endpoint,
        model_name=body.model_name,
        is_local=body.is_local,
        api_key=body.api_key,
    )
    return wrap(_view_out(view))


@router.post("/summary/test", response_model=Envelope[ConnectionTestOut])
async def test_summary_connection(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ok, message = await settings_service.test_connection(db)
    return wrap(ConnectionTestOut(ok=ok, message=message))


@router.delete("/summary/key", response_model=Envelope[SummaryModelSettingsOut])
async def delete_summary_api_key(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await settings_service.delete_api_key(db)
    return wrap(_view_out(await settings_service.get_settings_view(db)))
