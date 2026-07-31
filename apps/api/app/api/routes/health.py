"""sidecar 상태 확인 — Tauri 셸 전용(사용자 GUI에는 노출하지 않는다)."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

router = APIRouter()


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    try:
        row = await db.execute(text("SELECT version_num FROM alembic_version"))
        revision = row.scalar_one_or_none() or "none"
    except Exception:
        revision = "not-initialized"
    from app import __version__

    return {"status": "ok", "migrationRevision": revision, "sidecarVersion": __version__}
