"""로컬 AI 온보딩 서비스 — 모델 목록 뷰 조합 + 설정 활성화."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.services.local_ai import client, system
from app.services.local_ai import settings as st
from app.services.local_ai.settings import ModelSpec
from app.services.summary.factory import load_settings_row
from app.services.summary.settings_service import SummarySettingsView, get_settings_view


@dataclass
class ModelView:
    model: str  # 내부명(상세 보기 전용)
    tier: str
    label: str
    description: str
    approx_bytes: int
    installed: bool
    recommended: bool  # 기본 추천(8B)
    ram_advice: str  # recommended | selectable | warn | unknown
    disk_ok: bool  # 여유 공간이 충분한지
    required_bytes: int


@dataclass
class ModelsView:
    models: list[ModelView]
    default_model: str
    total_ram_bytes: int | None
    free_disk_bytes: int | None


def build_models_view() -> ModelsView:
    """카탈로그 + 설치 여부 + RAM/디스크 안내를 조합한다. Ollama 준비 상태에서 호출한다."""
    installed = {m.name for m in client.list_models()}
    ram = system.total_ram_bytes()
    free_disk = system.free_disk_bytes()
    views: list[ModelView] = []
    for spec in st.MODEL_CATALOG:
        required = st.required_disk_bytes(spec)
        views.append(
            ModelView(
                model=spec.model,
                tier=spec.tier,
                label=spec.label,
                description=spec.description,
                approx_bytes=spec.approx_bytes,
                installed=spec.model in installed,
                recommended=spec.model == st.DEFAULT_MODEL,
                ram_advice=st.ram_advice(spec.model, ram),
                # 디스크 정보를 못 읽으면(None) 막지 않는다(안내만 생략).
                disk_ok=(free_disk is None) or (free_disk >= required),
                required_bytes=required,
            )
        )
    return ModelsView(
        models=views,
        default_model=st.DEFAULT_MODEL,
        total_ram_bytes=ram,
        free_disk_bytes=free_disk,
    )


def spec_for(model: str) -> ModelSpec:
    spec = st.CATALOG_BY_MODEL.get(model)
    if spec is None:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)
    return spec


async def activate(
    db: AsyncSession, model: str, *, overwrite_external: bool
) -> SummarySettingsView:
    """연결된 로컬 모델을 요약/Q&A 설정에 저장한다. keyring에는 아무것도 남기지 않는다.

    기존 설정이 '외부 모델'이면 overwrite_external 확인이 있어야 덮어쓴다(없으면 409).
    """
    if model not in st.ALLOWED_MODELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)

    row = await load_settings_row(db)
    # enabled 여부와 무관하게 '외부 설정이 저장돼 있으면' 덮어쓰기 전 확인을 받는다
    # (잠시 꺼둔 외부 endpoint·모델을 조용히 날리지 않도록).
    is_external_now = bool(
        row is not None
        and row.provider_type == "openai_compatible"
        and not row.is_local
        and (row.endpoint or row.model_name)
    )
    if is_external_now and not overwrite_external:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "이미 외부 AI가 설정되어 있습니다. 로컬 AI로 바꿀까요?",
            status_code=409,
        )

    if row is None:
        from app.models.summary import SummarySettings

        row = SummarySettings()
        db.add(row)

    row.enabled = True
    row.provider_type = "openai_compatible"
    row.is_local = True
    row.endpoint = st.OLLAMA_OPENAI_BASE
    row.model_name = model
    # 로컬은 비밀이 없다 — keyring에 placeholder를 저장하지 않는다.
    # 기존에 외부 키가 남아 있어도 로컬 provider는 키를 쓰지 않는다(available: is_local).

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "설정을 저장하지 못했습니다. 다시 시도해 주세요.",
            status_code=500,
        ) from exc
    return await get_settings_view(db)
