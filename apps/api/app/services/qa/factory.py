"""Q&A 공급자 팩토리 — 요약 모델 설정(SummarySettings)+keyring을 그대로 재사용한다.

별도의 API 키 저장소·endpoint 설정을 만들지 않는다.
"""

import asyncio

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


async def _resolved_config(session: AsyncSession) -> ResolvedProviderConfig | None:
    row = await load_settings_row(session)
    if row is None or not row.enabled:
        return None
    return ResolvedProviderConfig(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        # keyring 읽기는 OS 자격증명 저장소를 치는 동기 I/O — 이벤트 루프를 막지 않게 위임.
        api_key=await asyncio.to_thread(secrets.get_api_key),
    )


async def get_qa_provider(session: AsyncSession) -> QaProvider:
    override = get_settings().qa_provider
    if override == "deterministic":
        return DeterministicQaProvider()
    if override == "disabled":
        return DisabledQaProvider()
    return build_qa_provider(await _resolved_config(session))


async def get_qa_streaming_provider(session: AsyncSession):
    """스트리밍 공급자. 요약 모델 설정·keyring을 그대로 재사용한다."""
    from app.services.qa.streaming import (
        DeterministicStreamingQaProvider,
        _DisabledStreaming,
        build_qa_streaming_provider,
    )

    override = get_settings().qa_provider
    if override == "deterministic":
        return DeterministicStreamingQaProvider()
    if override == "disabled":
        return _DisabledStreaming()
    return build_qa_streaming_provider(await _resolved_config(session))
