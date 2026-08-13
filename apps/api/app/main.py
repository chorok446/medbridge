import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.core.paths import get_path_provider
from app.services.system.backups import (
    enforce_total_budget,
    next_backup_path,
    prune_backups,
)

logger = get_logger(__name__)

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# 마이그레이션 전 백업(현재 DB 크기)과 이후 VACUUM 임시 파일(최대 현재 DB 크기)이
# 같은 볼륨에 한동안 공존한다. 파일시스템 메타데이터·WAL 여유까지 고려한 고정 안전폭.
MIGRATION_DISK_SAFETY_BYTES = 512 * 1024**2


def _disk_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _ensure_migration_disk_space(db_path: Path, backups_dir: Path) -> None:
    """파괴적 마이그레이션을 시작하기 전에 백업+VACUUM 공간을 fail-closed로 확인한다.

    백업과 DB가 다른 볼륨이면 각 볼륨의 필요량을 따로 본다. 경로는 오류에 넣지 않는다.
    Windows 계정명이 포함된 앱 데이터 경로가 UI·오류 보고서로 새는 것을 막기 위해서다.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    database_bytes = db_path.stat().st_size
    same_volume = db_path.parent.stat().st_dev == backups_dir.stat().st_dev

    if same_volume:
        required = database_bytes * 2 + MIGRATION_DISK_SAFETY_BYTES
        free = _disk_free_bytes(db_path.parent)
        if free < required:
            raise RuntimeError(
                "데이터베이스 업데이트에 필요한 디스크 공간이 부족합니다. "
                f"최소 {required / 1024**3:.1f}GB가 필요하지만 "
                f"{free / 1024**3:.1f}GB만 남아 있습니다."
            )
        return

    backup_required = database_bytes + MIGRATION_DISK_SAFETY_BYTES
    database_required = database_bytes + MIGRATION_DISK_SAFETY_BYTES
    backup_free = _disk_free_bytes(backups_dir)
    database_free = _disk_free_bytes(db_path.parent)
    if backup_free < backup_required or database_free < database_required:
        raise RuntimeError(
            "데이터베이스 업데이트에 필요한 디스크 공간이 부족합니다. "
            "앱 데이터와 백업 위치에 충분한 공간을 확보해 주세요."
        )


def _backup_sqlite_database(db_path: Path, backup_path: Path) -> None:
    """온라인 백업으로 main DB와 커밋된 WAL 프레임을 일관된 파일에 담는다."""
    backup_path.touch(exist_ok=False)
    source_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source,
            closing(sqlite3.connect(str(backup_path))) as destination,
        ):
            source.backup(destination)
            check = destination.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise


def _vacuum_sqlite_database(db_path: Path) -> None:
    """마이그레이션으로 비운 공간을 OS에 돌려준다.

    SQLite는 지운 페이지를 freelist에 넣어두고 파일을 줄이지 않는다(auto_vacuum=NONE).
    실기기에서 문서 6개(11,260페이지)를 지운 뒤에도 0.60GB가 파일에 그대로 남아 있었고,
    그 상태의 DB가 마이그레이션마다 통째로 백업돼 낭비가 증폭됐다.

    마이그레이션이 실제로 적용된 직후에만 부른다 — 매 기동마다 2.5GB를 통째로 다시
    쓰면 시작이 느려진다. VACUUM은 트랜잭션 안에서 돌 수 없어 alembic 밖에서 부른다.

    실패해도 삼킨다. 공간 회수는 부수적인 청소이고, 여기서 터지면 앱이 기동하지 못한다.
    """
    try:
        with closing(sqlite3.connect(str(db_path), isolation_level=None)) as conn:
            conn.execute("VACUUM")
        # stat()도 삼킴 안에 둔다 — 로그 한 줄의 실패가 앱 기동을 막으면 안 된다.
        logger.info("db_vacuumed", size_bytes=db_path.stat().st_size)
    except Exception:
        logger.warning("db_vacuum_failed")


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

        # 백업을 반쯤 쓴 뒤 ENOSPC로 실패하거나, 백업은 됐지만 VACUUM 임시 파일을
        # 만들지 못하는 상태를 피한다. 어떤 스키마 변경도 실행하기 전에 검사한다.
        _ensure_migration_disk_space(db_path, provider.backups_dir)
        backup = next_backup_path(provider.backups_dir, "pre-migration-", __version__)
        _backup_sqlite_database(db_path, backup)
        logger.info("db_backup_created", backup=backup.name)
        # 새 백업이 자리 잡은 뒤에 정리한다 — 먼저 지우면 백업이 실패했을 때
        # 되돌릴 사본만 없앤 꼴이 된다.
        prune_backups(provider.backups_dir, "pre-migration-")
        # 개수 상한은 접두사별이라 총량을 묶지 못한다 — 사본 크기가 DB에 비례해 자란다.
        enforce_total_budget(provider.backups_dir)
    elif db_path is None:
        logger.warning("db_backup_skipped_non_sqlite_url")

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    # 앱 내부 실행에서는 configure_logging()이 만든 stream+sidecar FileHandler를 보존한다.
    # 이 플래그가 없는 Alembic CLI 실행은 env.py의 기존 fileConfig 동작을 유지한다.
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    logger.info("migrations_applied", revision=head)
    if db_path is not None and current != head:
        _vacuum_sqlite_database(db_path)


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
    from app.api.routes import (
        documents,
        extraction,
        health,
        local_ai,
        ocr,
        profile,
        qa,
        reports,
        search,
        summary,
        system,
    )
    from app.api.routes import (
        settings as settings_routes,
    )

    configure_logging()
    settings = get_settings()
    app = FastAPI(title="MedBridge Sidecar", version="0.1.0", lifespan=lifespan)

    # 미들웨어 등록 순서가 실행 순서를 뒤집는다: Starlette는 나중에 등록한
    # 미들웨어를 더 바깥(먼저 실행)에 둔다. 토큰 검사를 먼저 등록하고
    # CORSMiddleware를 나중에 등록해, CORS가 전체 앱의 최외곽 래퍼가 되게
    # 한다 — 그래야 preflight(OPTIONS)뿐 아니라 토큰 검사가 반환하는 401
    # 응답까지도 CORSMiddleware를 통과하며 Access-Control-Allow-Origin이
    # 붙는다. 순서가 반대면(CORS를 먼저 등록) 토큰 검사가 CORS보다 바깥에
    # 있게 되어, preflight도 401로 막히고 그 401에는 CORS 헤더가 없어
    # 브라우저가 이를 "CORS 차단"으로 보고한다 (Windows 실기기에서 재현).
    @app.middleware("http")
    async def correlation_and_token_middleware(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        correlation_id_var.set(cid)

        # 로컬 API 보호: Tauri가 발급한 토큰 없이는 접근 불가 (미설정 시 개발 모드 —
        # production은 config 검증이 토큰 없는 기동 자체를 거부한다)
        # CORS preflight(OPTIONS)는 브라우저가 커스텀 헤더 없이 보내므로 인증 대상에서
        # 제외한다. CORS 우회를 위해 인증 자체를 끄는 것이 아니라, 이 메서드에
        # 한해서만 통과시키고 실제 GET/POST/PATCH/DELETE 요청은 그대로 검증한다.
        token = settings.medbridge_api_token
        if request.method != "OPTIONS" and token and request.url.path != "/health":
            supplied = request.headers.get("X-MedBridge-Token")
            # query 토큰은 iframe이 헤더를 못 보내는 파일 미리보기 경로에만 허용
            if supplied is None and request.url.path.endswith("/file"):
                supplied = request.query_params.get("token")
            if supplied != token:
                return _error_response(401, ErrorCode.UNAUTHORIZED, "인증되지 않은 요청입니다.")

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    # 개발 브라우저와 Tauri 웹뷰 오리진만 허용. allow_credentials=False +
    # 명시적 origin 목록(와일드카드 아님) 조합만 사용한다 — 쿠키를 쓰지 않고
    # 커스텀 헤더 토큰만 쓰므로 credentialed CORS는 필요 없고, 그 조합에서만
    # Starlette가 요청 origin이 목록에 있을 때만 정확히 반사(echo)한다.
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
        allow_headers=["X-MedBridge-Token", "Content-Type"],
    )

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
    app.include_router(ocr.router)
    app.include_router(search.router)
    app.include_router(summary.router)
    app.include_router(qa.router)
    app.include_router(local_ai.router)
    app.include_router(settings_routes.router)
    app.include_router(reports.router)
    app.include_router(system.router)
    return app


app = create_app()
