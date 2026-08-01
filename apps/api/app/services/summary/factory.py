"""요약 공급자 팩토리 — DB 설정 + keyring 키 + 테스트 override를 실제 공급자로 해석한다.

get_embedding_provider()·ocr_service.engine()과 동일한 단일 주입 지점.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.summary import SummarySettings
from app.services.summary import secrets
from app.services.summary.provider import (
    DeterministicSummaryProvider,
    DisabledSummaryProvider,
    SummaryProvider,
    build_summary_provider,
)


@dataclass
class ResolvedProviderConfig:
    enabled: bool
    provider_type: str
    endpoint: str | None
    model_name: str | None
    is_local: bool
    api_key: str | None


class DuplicateSummarySettingsError(RuntimeError):
    """단일 행이어야 하는 설정 테이블에 중복 행이 있을 때의 안전한 오류."""


async def load_settings_row(session: AsyncSession) -> SummarySettings | None:
    rows = (await session.execute(select(SummarySettings).limit(2))).scalars().all()
    if len(rows) > 1:
        # 어느 행도 임의로 고르거나 덮어쓰지 않는다. 행 값은 예외 메시지에 넣지 않는다.
        raise DuplicateSummarySettingsError
    return rows[0] if rows else None


async def get_summary_provider(session: AsyncSession) -> SummaryProvider:
    override = get_settings().summary_provider
    if override == "deterministic":
        return DeterministicSummaryProvider()
    if override == "disabled":
        return DisabledSummaryProvider()

    # override == "auto": DB 설정 + keyring 키로 실제 공급자를 만든다
    row = await load_settings_row(session)
    if row is None or not row.enabled:
        return DisabledSummaryProvider()
    config = ResolvedProviderConfig(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        api_key=secrets.get_api_key(),
    )
    return build_summary_provider(config)
