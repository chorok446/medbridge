"""사용자 오류 신고. Sprint 1에서는 구조화 로그로만 남긴다 (관리자는 로그에서 확인)."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.services.documents.service import get_owned_document
from app.utils.responses import wrap

router = APIRouter(prefix="/api/reports", tags=["reports"])
logger = get_logger(__name__)


class ReportRequest(CamelModel):
    document_id: uuid.UUID | None = None
    description: str = Field(default="", max_length=2000)


class ReportOut(CamelModel):
    received: bool


@router.post("", response_model=Envelope[ReportOut], status_code=201)
async def create_report(
    body: ReportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    failure_code = None
    if body.document_id is not None:
        doc = await get_owned_document(db, user, body.document_id, include_deleted=True)
        failure_code = doc.failure_code
    # sidecar.log에는 메타데이터만 남긴다 (오류 보고 zip에 포함되는 로그이므로
    # 사용자 자유 입력은 별도 파일에 기록하고 zip에는 넣지 않는다)
    logger.info(
        "user_error_report",
        document_id=str(body.document_id) if body.document_id else None,
        failure_code=failure_code,
        description_length=len(body.description),
    )
    if body.description.strip():
        from datetime import UTC, datetime

        from app.core.paths import get_path_provider

        reports_log = get_path_provider().logs_dir / "user-reports.log"
        reports_log.parent.mkdir(parents=True, exist_ok=True)
        with reports_log.open("a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now(UTC).isoformat()} doc={body.document_id} "
                f"code={failure_code}\n{body.description[:2000]}\n---\n"
            )
    return wrap(ReportOut(received=True))
