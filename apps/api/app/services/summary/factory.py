"""요약 공급자 팩토리 — DB 설정 + keyring 키 + 테스트 override를 실제 공급자로 해석한다.

get_embedding_provider()·ocr_service.engine()과 동일한 단일 주입 지점.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    provider_identity_digest: str | None = None
    model_digest: str | None = None
    provider_identity_generation: str | None = None


class DuplicateSummarySettingsError(RuntimeError):
    """단일 행이어야 하는 설정 테이블에 중복 행이 있을 때의 안전한 오류."""


# 사용자에게 그대로 보여줄 안내. 읽기 경로는 어느 행이 옳은지 알 수 없으므로,
# 사용자가 원하는 값을 직접 말해 주는 저장 경로로 유도한다(거기서 중복이 정리된다).
DUPLICATE_SETTINGS_MESSAGE = (
    "AI 모델 설정이 중복 저장되어 어느 쪽이 맞는지 확인할 수 없어요. "
    "설정 화면에서 값을 다시 저장하면 정리됩니다."
)

# Ollama /api/tags 조회는 provider worker가 async guard를 기다리는 동안 실행된다. 기본
# executor에 다시 제출하면 keyring 때와 같은 재귀 포화 교착이 생기므로 별도 pool을 쓴다.
_PROVIDER_METADATA_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="summary-provider-metadata"
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


async def get_summary_provider(
    session: AsyncSession, *, resolve_identity: bool = False
) -> SummaryProvider:
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
    # 외부 credential은 전용 executor에서 한 번에 읽는다. 모델 worker가 전송 직전
    # run_coroutine_threadsafe guard를 기다릴 때 기본 executor가 포화돼도 교착되지 않는다.
    api_key, provider_identity_digest = (
        (None, None)
        if row.is_local
        else await secrets.run_in_keyring_thread(secrets.get_api_key_with_identity)
    )
    model_digest: str | None = None
    provider_identity_generation: str | None = None
    if row.is_local and resolve_identity:
        from app.services.local_ai import client as local_ai_client
        from app.services.summary.endpoint import is_ollama_native_endpoint

        # 같은 model tag가 pull로 교체돼도 manifest digest가 달라지면 과거 노드를 쓰지
        # 않는다. 조회가 불가하면 provider 인스턴스별 fail-safe generation을 써서 이번
        # 실행은 완료할 수 있지만 과거/다른 실행의 checkpoint와는 절대 섞이지 않는다.
        # 실행 중 actual HTTP 전송 guard도 이 경로를 다시 호출한다. 즉 /api/tags를 요청별
        # 최신 상태로 확인하며, 조회 자체는 local_ai STATUS_TIMEOUT_SEC(4초)로 제한된다.
        # 짧은 TTL 캐시는 tag 교체 직후 이전 모델 결과를 보내는 창을 만들므로 두지 않는다.
        # LM Studio 등 다른 loopback 서버의 모델과 별도 Ollama 카탈로그를 섞지 않는다.
        if is_ollama_native_endpoint(row.endpoint or "", is_local=True):
            model_digest = await asyncio.get_running_loop().run_in_executor(
                _PROVIDER_METADATA_EXECUTOR,
                local_ai_client.installed_model_digest,
                row.model_name or "",
            )
        if not model_digest:
            provider_identity_generation = uuid.uuid4().hex
    config = ResolvedProviderConfig(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        # 로컬 공급자는 키가 필요 없다. 장시간 요약은 실제 전송 직전 설정을 매번 다시
        # 확인하므로 로컬에서도 keyring을 읽으면 수백~수천 번 불필요한 OS I/O가 생긴다.
        api_key=api_key,
        provider_identity_digest=provider_identity_digest,
        model_digest=model_digest,
        provider_identity_generation=provider_identity_generation,
    )
    return build_summary_provider(config)
