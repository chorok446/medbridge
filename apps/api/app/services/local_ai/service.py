"""로컬 AI 온보딩 서비스 — 모델 목록 뷰 조합 + 설정 활성화."""

import asyncio
import sqlite3
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var, get_logger
from app.models.summary import SummarySettings
from app.services.local_ai import client, system
from app.services.local_ai import settings as st
from app.services.local_ai.settings import ModelSpec
from app.services.summary.factory import DuplicateSummarySettingsError, load_settings_row
from app.services.summary.settings_service import SummarySettingsView, get_settings_view

logger = get_logger(__name__)

_ACTIVATE_MAX_ATTEMPTS = 2
_ACTIVATE_RETRY_DELAYS_SECONDS = (0.15,)
_OPERATION = "local_ai_activate"


@dataclass(frozen=True)
class PersistenceFailure:
    category: str
    retryable: bool
    sqlite_error_code: int | None
    sqlite_primary_code: int | None
    exception_type: str


def classify_persistence_failure(exc: Exception) -> PersistenceFailure:
    """DB 예외를 원문·SQL·경로 없이 안전한 범주로 축약한다."""
    original: object = exc
    seen: set[int] = set()
    for _ in range(5):
        if id(original) in seen:
            break
        seen.add(id(original))
        if getattr(original, "sqlite_errorcode", None) is not None:
            break
        next_exc = getattr(original, "orig", None) or getattr(original, "__cause__", None)
        if next_exc is None:
            break
        original = next_exc

    raw_code = getattr(original, "sqlite_errorcode", None)
    code = raw_code if isinstance(raw_code, int) else None
    primary = code & 0xFF if code is not None else None

    category = "unknown_persistence_error"
    retryable = False
    if isinstance(original, DuplicateSummarySettingsError):
        category = "duplicate_settings"
    elif primary == sqlite3.SQLITE_BUSY:
        category = "db_locked"
        retryable = True
    elif primary == sqlite3.SQLITE_LOCKED:
        # 같은 connection/shared-cache 충돌은 자동 재시도하지 않지만, 새 요청은 가능하다.
        category = "db_locked"
        retryable = True
    elif primary == sqlite3.SQLITE_READONLY:
        category = "db_readonly"
    elif primary == sqlite3.SQLITE_SCHEMA:
        category = "db_schema_mismatch"
    elif primary in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        category = "db_integrity"
    elif primary == sqlite3.SQLITE_CONSTRAINT or isinstance(exc, IntegrityError):
        category = "db_integrity"
    elif primary in (
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PROTOCOL,
    ):
        category = "db_io"
    elif primary == sqlite3.SQLITE_ERROR:
        # SQLite는 누락 테이블/컬럼에도 일반 SQLITE_ERROR(1)를 쓴다. 문자열은 분류에만
        # 사용하고 로그·응답에는 절대 싣지 않는다.
        message = str(original).lower()
        schema_markers = ("no such table", "no such column", "has no column")
        if any(marker in message for marker in schema_markers):
            category = "db_schema_mismatch"

    return PersistenceFailure(
        category=category,
        retryable=retryable,
        sqlite_error_code=code,
        sqlite_primary_code=primary,
        exception_type=type(original).__name__,
    )


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


def _persistence_log_fields(
    failure: PersistenceFailure,
    *,
    stage: str,
    attempt: int,
    committed: bool = False,
) -> dict[str, object]:
    return {
        "operation": _OPERATION,
        "failureCategory": failure.category,
        "stage": stage,
        "sqliteErrorCode": failure.sqlite_error_code,
        "sqlitePrimaryCode": failure.sqlite_primary_code,
        "exceptionType": failure.exception_type,
        "attempt": attempt,
        "maxAttempts": _ACTIVATE_MAX_ATTEMPTS,
        "retryable": failure.retryable,
        "committed": committed,
        "correlationId": correlation_id_var.get(),
    }


def _persistence_app_error(failure: PersistenceFailure, stage: str) -> AppError:
    details: dict[str, object] = {"failureCategory": failure.category, "stage": stage}
    if failure.sqlite_error_code is not None:
        details["sqliteErrorCode"] = failure.sqlite_error_code
    if failure.sqlite_primary_code is not None:
        details["sqlitePrimaryCode"] = failure.sqlite_primary_code
    if failure.category == "db_locked":
        return AppError(
            ErrorCode.DB_LOCKED,
            "다른 문서 작업이 저장 중이에요. 잠시 후 다시 시도해 주세요.",
            status_code=503,
            retryable=True,
            details=details,
        )
    return AppError(
        ErrorCode.INTERNAL_ERROR,
        "설정을 저장하지 못했습니다. 다시 시도해 주세요.",
        status_code=500,
        details=details,
    )


async def _sleep_before_retry(delay: float) -> None:
    """테스트에서 lock 해제 시점을 제어할 수 있는 단일 대기 지점."""
    await asyncio.sleep(delay)


async def _rollback_after_failure(
    db: AsyncSession, *, original: Exception, attempt: int
) -> None:
    try:
        await db.rollback()
    except Exception as rollback_exc:
        failure = classify_persistence_failure(rollback_exc)
        logger.error(
            "local_ai_activate_rollback_failed",
            **_persistence_log_fields(failure, stage="rollback", attempt=attempt),
        )
        raise _persistence_app_error(failure, "rollback") from original


async def _persist_activation_with_retry(
    db: AsyncSession, model: str, *, overwrite_external: bool
) -> None:
    """설정 조회부터 commit까지를 SQLITE_BUSY일 때 한 번만 다시 실행한다."""
    for attempt in range(1, _ACTIVATE_MAX_ATTEMPTS + 1):
        stage = "load_settings"
        try:
            row = await load_settings_row(db)

            stage = "validate_existing"
            # enabled 여부와 무관하게 외부 설정이 저장돼 있으면 덮어쓰기 확인을 받는다.
            is_external_now = bool(
                row is not None
                and row.provider_type == "openai_compatible"
                and not row.is_local
                and (row.endpoint or row.model_name)
            )
            if is_external_now and not overwrite_external:
                raise AppError(
                    ErrorCode.EXTERNAL_AI_OVERWRITE_REQUIRED,
                    "이미 외부 AI가 설정되어 있습니다. 로컬 AI로 바꿀까요?",
                    status_code=409,
                    details={
                        "failureCategory": "external_settings_conflict",
                        "conflictType": "external_ai_overwrite_required",
                    },
                )

            stage = "create_or_update_row"
            if row is None:
                row = SummarySettings()
                db.add(row)
            row.enabled = True
            row.provider_type = "openai_compatible"
            row.is_local = True
            row.endpoint = st.OLLAMA_OPENAI_BASE
            row.model_name = model

            # commit의 암묵적 flush와 분리해 실제 잠금 실패 단계를 기록한다.
            stage = "flush"
            await db.flush()
            stage = "commit"
            await db.commit()
            return
        except AppError:
            await db.rollback()
            raise
        except Exception as exc:
            failure = classify_persistence_failure(exc)
            await _rollback_after_failure(db, original=exc, attempt=attempt)
            should_retry = (
                failure.sqlite_primary_code == sqlite3.SQLITE_BUSY
                and attempt < _ACTIVATE_MAX_ATTEMPTS
            )
            if should_retry:
                logger.warning(
                    "local_ai_activate_retry",
                    **_persistence_log_fields(failure, stage=stage, attempt=attempt),
                )
                await _sleep_before_retry(_ACTIVATE_RETRY_DELAYS_SECONDS[attempt - 1])
                continue

            logger.error(
                "local_ai_activate_failed",
                **_persistence_log_fields(failure, stage=stage, attempt=attempt),
            )
            raise _persistence_app_error(failure, stage) from exc

    raise RuntimeError("unreachable")


async def activate(
    db: AsyncSession, model: str, *, overwrite_external: bool
) -> SummarySettingsView:
    """연결된 로컬 모델을 요약/Q&A 설정에 저장한다. keyring에는 아무것도 남기지 않는다.

    기존 설정이 '외부 모델'이면 overwrite_external 확인이 있어야 덮어쓴다(없으면 409).
    """
    if model not in st.ALLOWED_MODELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)

    # 로컬은 비밀이 없다 — keyring에 placeholder를 저장하지 않는다.
    # 기존에 외부 키가 남아 있어도 로컬 provider는 키를 쓰지 않는다(available: is_local).
    await _persist_activation_with_retry(
        db, model, overwrite_external=overwrite_external
    )
    try:
        return await get_settings_view(db)
    except Exception as exc:
        cause = classify_persistence_failure(exc)
        try:
            await db.rollback()
        except Exception as rollback_exc:
            rollback_failure = classify_persistence_failure(rollback_exc)
            logger.error(
                "local_ai_activate_rollback_failed",
                **_persistence_log_fields(
                    rollback_failure,
                    stage="rollback_after_view",
                    attempt=1,
                    committed=True,
                ),
            )
        report = _persistence_log_fields(
            cause,
            stage="reload_settings_view",
            attempt=1,
            committed=True,
        )
        report.update(
            failureCategory="post_commit_view_failed",
            causeCategory=cause.category,
            maxAttempts=1,
            retryable=False,
        )
        logger.error("local_ai_activate_post_commit_view_failed", **report)
        raise AppError(
            ErrorCode.POST_COMMIT_VIEW_FAILED,
            "설정은 저장했지만 상태를 다시 확인하지 못했습니다. 설정 화면을 다시 열어 주세요.",
            status_code=500,
            details={
                "failureCategory": "post_commit_view_failed",
                "stage": "reload_settings_view",
                "committed": True,
            },
        ) from exc
