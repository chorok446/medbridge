"""요약 공급자 팩토리 — DB 설정 + keyring 키 + 테스트 override를 실제 공급자로 해석한다.

get_embedding_provider()·ocr_service.engine()과 동일한 단일 주입 지점.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
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


# 사용자에게 그대로 보여줄 안내. 읽기 경로는 어느 행이 옳은지 알 수 없으므로,
# 사용자가 원하는 값을 직접 말해 주는 저장 경로로 유도한다(거기서 중복이 정리된다).
DUPLICATE_SETTINGS_MESSAGE = (
    "AI 모델 설정이 중복 저장되어 어느 쪽이 맞는지 확인할 수 없어요. "
    "설정 화면에서 값을 다시 저장하면 정리됩니다."
)


def duplicate_settings_error() -> AppError:
    """중복 설정을 사용자에게 보여줄 수 있는 오류로 바꾼다.

    이게 없으면 RuntimeError가 그대로 올라가 500 INTERNAL_ERROR가 되고, 화면에는
    "문제가 발생했습니다"만 떠서 사용자가 할 수 있는 일을 알 수 없다.
    """
    return AppError(
        ErrorCode.DUPLICATE_SETTINGS,
        DUPLICATE_SETTINGS_MESSAGE,
        status_code=409,
        details={"failureCategory": "duplicate_settings"},
    )


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
    try:
        row = await load_settings_row(session)
    except DuplicateSummarySettingsError as exc:
        raise duplicate_settings_error() from exc
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
