"""Q&A 공급자 팩토리 — 요약 모델 설정(SummarySettings)+keyring을 그대로 재사용한다.

별도의 API 키 저장소·endpoint 설정을 만들지 않는다.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.qa.provider import (
    DeterministicQaProvider,
    DisabledQaProvider,
    QaProvider,
    build_qa_provider,
)
from app.services.summary import secrets
from app.services.summary.factory import ResolvedProviderConfig, load_settings_row


async def get_qa_provider(session: AsyncSession) -> QaProvider:
    override = get_settings().qa_provider
    if override == "deterministic":
        return DeterministicQaProvider()
    if override == "disabled":
        return DisabledQaProvider()

    row = await load_settings_row(session)
    if row is None or not row.enabled:
        return DisabledQaProvider()
    config = ResolvedProviderConfig(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        api_key=secrets.get_api_key(),
    )
    return build_qa_provider(config)
