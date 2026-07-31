import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.core.paths import get_path_provider

logger = get_logger(__name__)

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _current_and_head_revision() -> tuple[str | None, str]:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head() or ""

    engine = create_engine(get_settings().database_url_sync)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    return current, head


def run_migrations() -> None:
    """앱 시작 시 자동 마이그레이션. 실행 전 DB 파일을 백업한다.

    실패 시 예외를 올려 앱이 정상 상태로 기동되지 않게 한다.
    """
    from alembic.config import Config

    from alembic import command

    provider = get_path_provider()
    provider.ensure_directories()

    current, head = _current_and_head_revision()
    # 백업은 마이그레이션이 실제 적용되는 DB 파일을 대상으로 한다 (DATABASE_URL override 포함)
    sync_url = get_settings().database_url_sync
    db_path = (
        Path(sync_url.removeprefix("sqlite:///")) if sync_url.startswith("sqlite:///") else None
    )
    if db_path is not None and db_path.is_file() and current != head:
        from app import __version__

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = provider.backups_dir / f"pre-migration-{__version__}-{stamp}.db"
        shutil.copy2(db_path, backup)
        logger.info("db_backup_created", backup=backup.name)
    elif db_path is None:
        logger.warning("db_backup_skipped_non_sqlite_url")

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")
    logger.info("migrations_applied", revision=head)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    import os

    from app.services.system import runtime
    from app.services.tasks.runner import get_task_runner

    # 중복 sidecar 실행 방지 + stale runtime 파일 정리
    runtime.acquire_single_instance(int(os.environ.get("MEDBRIDGE_BOUND_PORT", "0")))
    try:
        await asyncio.to_thread(run_migrations)
        recovered = await get_task_runner().recover_interrupted()
        logger.info("sidecar_ready", recovered_jobs=recovered)
        yield
        await get_task_runner().drain()
    finally:
        runtime.release_single_instance()


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: object = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details,
            },
            "meta": {"correlationId": correlation_id_var.get()},
        },
    )


def create_app() -> FastAPI:
    from app.api.routes import documents, extraction, health, profile, reports, system

    configure_logging()
    settings = get_settings()
    app = FastAPI(title="MedBridge Sidecar", version="0.1.0", lifespan=lifespan)

    # 개발 브라우저와 Tauri 웹뷰 오리진만 허용
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "http://localhost:3000",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_and_token_middleware(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        correlation_id_var.set(cid)

        # 로컬 API 보호: Tauri가 발급한 토큰 없이는 접근 불가 (미설정 시 개발 모드 —
        # production은 config 검증이 토큰 없는 기동 자체를 거부한다)
        token = settings.medbridge_api_token
        if token and request.url.path != "/health":
            supplied = request.headers.get("X-MedBridge-Token")
            # query 토큰은 iframe이 헤더를 못 보내는 파일 미리보기 경로에만 허용
            if supplied is None and request.url.path.endswith("/file"):
                supplied = request.query_params.get("token")
            if supplied != token:
                return _error_response(401, ErrorCode.UNAUTHORIZED, "인증되지 않은 요청입니다.")

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(
            exc.status_code, exc.code, exc.message, retryable=exc.retryable, details=exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422,
            ErrorCode.VALIDATION_FAILED,
            "요청 값이 올바르지 않습니다.",
            details=[{"loc": [str(x) for x in e["loc"]], "type": e["type"]} for e in exc.errors()],
        )

    @app.exception_handler(Exception)
    async def internal_handler(_request: Request, exc: Exception) -> JSONResponse:
        # 내부 예외 메시지·스택은 사용자에게 노출하지 않는다
        logger.error("unhandled_error", error_type=type(exc).__name__)
        return _error_response(
            500,
            ErrorCode.INTERNAL_ERROR,
            "문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            retryable=True,
        )

    app.include_router(health.router)
    app.include_router(profile.router)
    app.include_router(documents.router)
    app.include_router(extraction.router)
    app.include_router(reports.router)
    app.include_router(system.router)
    return app


app = create_app()
