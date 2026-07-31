from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """liveness — 프로세스 생존만 확인."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict:
    """readiness — DB·Redis·객체 저장소 연결 확인."""
    checks: dict[str, str] = {}

    await db.execute(text("SELECT 1"))
    checks["database"] = "ok"

    # 현재 적용된 마이그레이션 revision (업데이트 진단용)
    try:
        row = await db.execute(text("SELECT version_num FROM alembic_version"))
        checks["migration_revision"] = row.scalar_one_or_none() or "none"
    except Exception:
        checks["migration_revision"] = "not-initialized"

    import redis as redis_lib

    r = redis_lib.Redis.from_url(get_settings().redis_url, socket_timeout=3)
    try:
        r.ping()
        checks["redis"] = "ok"
    finally:
        r.close()

    from app.services.documents.storage import internal_client

    await run_in_threadpool(
        internal_client().head_bucket, Bucket=get_settings().minio_bucket_originals
    )
    checks["object_storage"] = "ok"

    return {"status": "ok", "checks": checks}
