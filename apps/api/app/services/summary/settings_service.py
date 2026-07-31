"""요약 모델 설정 조회·저장 + keyring API 키 관리 + 연결 확인.

API 키는 절대 DB·응답·로그에 노출하지 않는다(설정됨/미설정 불리언만).
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.models.summary import SummarySettings
from app.services.summary import secrets
from app.services.summary.factory import ResolvedProviderConfig, load_settings_row
from app.services.summary.provider import (
    ChunkInput,
    GroupRequest,
    build_summary_provider,
)

# deterministic은 테스트 전용 공급자라 공개 설정 API로는 선택할 수 없다
# (테스트는 summary_settings 행을 직접 넣거나 config override를 쓴다).
VALID_PROVIDER_TYPES = ("disabled", "openai_compatible")


@dataclass
class SummarySettingsView:
    enabled: bool
    provider_type: str
    endpoint: str | None
    model_name: str | None
    is_local: bool
    has_api_key: bool


async def _get_or_create(db: AsyncSession) -> SummarySettings:
    row = await load_settings_row(db)
    if row is None:
        row = SummarySettings()
        db.add(row)
        await db.flush()
    return row


async def get_settings_view(db: AsyncSession) -> SummarySettingsView:
    row = await load_settings_row(db)
    if row is None:
        return SummarySettingsView(
            enabled=False,
            provider_type="disabled",
            endpoint=None,
            model_name=None,
            is_local=False,
            has_api_key=secrets.has_api_key(),
        )
    return SummarySettingsView(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        has_api_key=secrets.has_api_key(),
    )


async def update_settings(
    db: AsyncSession,
    *,
    enabled: bool | None,
    provider_type: str | None,
    endpoint: str | None,
    model_name: str | None,
    is_local: bool | None,
    api_key: str | None,
) -> SummarySettingsView:
    if provider_type is not None and provider_type not in VALID_PROVIDER_TYPES:
        raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 공급자 유형입니다.", status_code=422)
    row = await _get_or_create(db)
    if enabled is not None:
        row.enabled = enabled
    if provider_type is not None:
        row.provider_type = provider_type
    if endpoint is not None:
        row.endpoint = endpoint.strip() or None
    if model_name is not None:
        row.model_name = model_name.strip() or None
    if is_local is not None:
        row.is_local = is_local
    # api_key는 keyring에만 저장 — DB·로그에 남기지 않는다. 빈 문자열이면 삭제.
    if api_key is not None:
        try:
            if api_key.strip():
                secrets.set_api_key(api_key.strip())
            else:
                secrets.delete_api_key()
        except Exception as exc:
            # OS 자격 증명 저장소를 쓸 수 없는 환경 — 평문 저장으로 우회하지 않고 실패시킨다.
            await db.rollback()
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "이 기기에서 API 키를 안전하게 저장할 수 없습니다.",
                status_code=500,
            ) from exc
    await db.commit()
    return await get_settings_view(db)


async def delete_api_key(db: AsyncSession) -> None:
    secrets.delete_api_key()


async def test_connection(db: AsyncSession) -> tuple[bool, str]:
    """저장된 설정으로 공급자를 만들어 최소 호출을 시도한다. 키 값은 반환하지 않는다."""
    row = await load_settings_row(db)
    if row is None or not row.enabled:
        return False, "요약 모델이 아직 켜져 있지 않습니다."
    config = ResolvedProviderConfig(
        enabled=row.enabled,
        provider_type=row.provider_type,
        endpoint=row.endpoint,
        model_name=row.model_name,
        is_local=row.is_local,
        api_key=secrets.get_api_key(),
    )
    provider = build_summary_provider(config)
    if not provider.available:
        return False, "설정이 완전하지 않습니다. endpoint·모델명·API 키를 확인해 주세요."
    try:
        provider.summarize_group(
            GroupRequest(
                group_id="probe",
                section_title=None,
                chunks=[ChunkInput("probe", None, "연결 확인용 짧은 문장입니다.", 1, 1)],
                learner_level="nursing_student",
                language="ko",
            )
        )
    except Exception:
        return False, "모델 서비스에 연결하지 못했습니다. 설정을 확인해 주세요."
    return True, "연결에 성공했습니다."
