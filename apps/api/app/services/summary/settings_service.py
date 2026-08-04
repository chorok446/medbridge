"""요약 모델 설정 조회·저장 + keyring API 키 관리 + 연결 확인.

API 키는 절대 DB·응답·로그에 노출하지 않는다(설정됨/미설정 불리언만).
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.summary import SummarySettings
from app.services.summary import secrets
from app.services.summary.endpoint import SummaryNetworkError, validate_endpoint
from app.services.summary.factory import (
    DuplicateSummarySettingsError,
    ResolvedProviderConfig,
    duplicate_settings_error,
    load_settings_row,
)
from app.services.summary.provider import (
    ChunkInput,
    GroupRequest,
    build_summary_provider,
)
from app.services.summary.settings import CONNECTION_TEST_TIMEOUT_SEC

# deterministic은 테스트 전용 공급자라 공개 설정 API로는 선택할 수 없다
# (테스트는 summary_settings 행을 직접 넣거나 config override를 쓴다).
VALID_PROVIDER_TYPES = ("disabled", "openai_compatible")

logger = get_logger(__name__)


@dataclass
class SummarySettingsView:
    enabled: bool
    provider_type: str
    endpoint: str | None
    model_name: str | None
    is_local: bool
    has_api_key: bool


async def _get_or_create(db: AsyncSession) -> SummarySettings:
    """저장용 단일 행을 얻는다. 중복이 있으면 여기서 정리한다.

    읽기 경로는 어느 행이 옳은지 알 수 없어 임의로 고르지 않는다. 하지만 저장은
    사용자가 원하는 값을 직접 말해 주는 자리라, 가장 최근 행만 남기고 접어도 추측이
    아니다 — 게다가 그 값은 바로 아래에서 사용자 입력으로 덮어써진다. 이 정리가
    없으면 중복이 생긴 순간 설정 화면조차 열리지 않아 UI만으로는 복구할 수 없다.
    """
    try:
        row = await load_settings_row(db)
    except DuplicateSummarySettingsError:
        rows = (
            await db.execute(
                select(SummarySettings).order_by(SummarySettings.updated_at.desc())
            )
        ).scalars().all()
        row = rows[0]
        for stale in rows[1:]:
            await db.delete(stale)
        await db.flush()
        logger.warning("summary_settings_duplicates_collapsed", removed=len(rows) - 1)
    if row is None:
        row = SummarySettings()
        db.add(row)
        await db.flush()
    return row


async def get_settings_view(db: AsyncSession) -> SummarySettingsView:
    try:
        row = await load_settings_row(db)
    except DuplicateSummarySettingsError as exc:
        raise duplicate_settings_error() from exc
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

    # 적용될 최종 endpoint·is_local·provider_type을 먼저 계산한다(검증 기준).
    eff_is_local = is_local if is_local is not None else row.is_local
    eff_provider = provider_type if provider_type is not None else row.provider_type
    raw_endpoint = endpoint.strip() if endpoint is not None else (row.endpoint or "")
    normalized_endpoint: str | None = row.endpoint

    # openai_compatible endpoint는 keyring을 건드리기 전에 엄격히 검증·정규화한다.
    if eff_provider == "openai_compatible" and raw_endpoint:
        try:
            normalized_endpoint = validate_endpoint(raw_endpoint, is_local=eff_is_local)
        except SummaryNetworkError as exc:
            raise AppError(
                ErrorCode.VALIDATION_FAILED, exc.user_message, status_code=422
            ) from exc
    elif endpoint is not None:
        normalized_endpoint = raw_endpoint or None

    if enabled is not None:
        row.enabled = enabled
    if provider_type is not None:
        row.provider_type = provider_type
    if endpoint is not None or normalized_endpoint != row.endpoint:
        row.endpoint = normalized_endpoint
    if model_name is not None:
        row.model_name = model_name.strip() or None
    if is_local is not None:
        row.is_local = is_local

    # api_key는 keyring에만 저장 — DB·로그에 남기지 않는다. 빈 문자열이면 삭제.
    # 부분 성공 방지: 이전 키를 먼저 기억하고, keyring을 건드리기 직전 mutated 플래그를
    # 세워 부분 변경(변경 후 실패 보고 포함)도 반드시 보상 대상이 되게 한다.
    previous_key: str | None = secrets.get_api_key() if api_key is not None else None
    key_mutated = False
    if api_key is not None:
        try:
            key_mutated = True  # 호출 자체가 자격 증명을 바꿀 수 있으므로 먼저 표시
            if api_key.strip():
                secrets.set_api_key(api_key.strip())
            else:
                secrets.delete_api_key()
        except Exception as exc:
            await db.rollback()
            _restore_key(previous_key)  # 부분 변경 되돌리기(실패 시 명확한 오류)
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "이 기기에서 API 키를 안전하게 저장할 수 없습니다.",
                status_code=500,
            ) from exc
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if key_mutated:  # keyring을 이전 값으로 되돌려 DB·keyring 불일치를 막는다
            _restore_key(previous_key)
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "설정을 저장하지 못했습니다. 다시 시도해 주세요.",
            status_code=500,
        ) from exc
    return await get_settings_view(db)


def _restore_key(previous_key: str | None) -> None:
    """keyring을 이전 값으로 되돌린다. 되돌리기까지 실패하면 불일치를 명확히 알린다
    (조용히 삼키지 않는다 — 사용자가 재시도·재설정하도록)."""
    try:
        if previous_key is not None:
            secrets.set_api_key(previous_key)
        else:
            secrets.delete_api_key()
        # 되돌린 값이 실제로 반영됐는지 확인
        if secrets.get_api_key() != previous_key:
            raise RuntimeError("keyring restore verification failed")
    except Exception as exc:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "설정 저장에 실패했고 이전 API 키 복원도 실패했습니다. 앱 설정에서 요약 모델 "
            "키를 다시 확인해 주세요.",
            status_code=500,
        ) from exc


async def delete_api_key(db: AsyncSession) -> None:
    secrets.delete_api_key()


async def test_connection(db: AsyncSession) -> tuple[bool, str]:
    """저장된 설정으로 공급자를 만들어 최소 호출을 시도한다. 키 값은 반환하지 않는다."""
    try:
        row = await load_settings_row(db)
    except DuplicateSummarySettingsError as exc:
        raise duplicate_settings_error() from exc
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
    provider = build_summary_provider(config, timeout=CONNECTION_TEST_TIMEOUT_SEC)
    if not provider.available:
        return False, "설정이 완전하지 않습니다. endpoint·모델명·API 키를 확인해 주세요."
    try:
        # 연결 확인은 문서 원문을 전송하지 않는다 — 고정된 짧은 프로브 문장만 보낸다.
        provider.summarize_group(
            GroupRequest(
                group_id="probe",
                section_title=None,
                chunks=[ChunkInput("probe", None, "연결 확인용 짧은 문장입니다.", 1, 1)],
                learner_level="nursing_student",
                language="ko",
            )
        )
    except SummaryNetworkError as exc:
        # 오류 범주만 사용자 메시지로 — 외부 서버 원문·stack trace·키는 노출하지 않는다.
        return False, exc.user_message
    except Exception:
        return False, "모델 서비스에 연결하지 못했습니다. 설정을 확인해 주세요."
    return True, "연결에 성공했습니다. 실제 요약 품질은 문서에 따라 다를 수 있어요."
