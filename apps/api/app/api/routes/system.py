"""업데이트 준비·오류 보고 데이터 — Tauri 셸 전용 표면 (사용자 GUI에 원문 비노출)."""

import platform
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.logging import get_logger
from app.core.paths import get_path_provider
from app.db.session import get_db
from app.models.document import Document, DocumentJob
from app.schemas.common import CamelModel, Envelope
from app.services.system import runtime
from app.utils.responses import wrap

router = APIRouter(prefix="/api/system", tags=["system"])
logger = get_logger(__name__)


class PrepareUpdateOut(CamelModel):
    ready: bool
    backup_file: str | None


@router.post("/prepare-update", response_model=Envelope[PrepareUpdateOut])
async def prepare_update() -> dict:
    """업데이트 직전 호출: 새 작업 차단 → 진행 중 요청·작업 완전 종료 대기 → checkpoint+백업."""
    runtime.set_updating(True)
    await runtime.wait_for_quiescence()
    backup = runtime.checkpoint_and_backup()
    return wrap(PrepareUpdateOut(ready=True, backup_file=backup))


@router.post("/resume", status_code=204)
async def resume() -> None:
    """사용자가 업데이트를 취소('나중에')한 경우 작업 차단 해제."""
    runtime.set_updating(False)


@router.get("/error-report", response_model=Envelope[dict])
async def error_report(db: AsyncSession = Depends(get_db)) -> dict:
    """오류 보고서용 데이터. PDF 원문·파일명·토큰·DB 전체는 포함하지 않는다."""
    try:
        revision = (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    except Exception:
        revision = None

    status_counts = dict(
        (row[0].value, row[1])
        for row in (
            await db.execute(
                select(Document.processing_status, func.count()).group_by(
                    Document.processing_status
                )
            )
        ).all()
    )
    recent_failures = [
        {"failureCode": row[0], "occurredAt": row[1].isoformat() if row[1] else None}
        for row in (
            await db.execute(
                select(DocumentJob.failure_code, DocumentJob.completed_at)
                .where(DocumentJob.failure_code.is_not(None))
                .order_by(DocumentJob.created_at.desc())
                .limit(20)
            )
        ).all()
    ]

    # sidecar.log만 포함한다 — user-reports.log(사용자 자유 입력)는 개인정보가
    # 섞일 수 있어 오류 보고서에 절대 넣지 않는다
    log_tail: list[str] = []
    log_file = get_path_provider().logs_dir / "sidecar.log"
    if log_file.is_file():
        log_tail = log_file.read_text(errors="replace").splitlines()[-200:]

    report: dict[str, Any] = {
        "sidecarVersion": __version__,
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "migrationRevision": revision,
        "documentStatusCounts": status_counts,
        "recentFailureCodes": recent_failures,
        "logTail": log_tail,
    }
    return wrap(report)
