"""요약 API — 생성/상태/조회/재시도/취소/삭제.

기술 정보(chunk id·모델명·토큰·원시 점수)는 응답에 노출하지 않는다. 출처는 사용자
네비게이션용 source_refs(page/bbox)만 내려준다.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.logging import correlation_id_var
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.services.documents.service import get_owned_document
from app.services.summary import service as summary_service
from app.services.summary.settings import DEFAULT_LEARNER_LEVEL
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["summary"])


class SummaryCreateRequest(CamelModel):
    learner_level: str = Field(default=DEFAULT_LEARNER_LEVEL)
    language: str = Field(default="ko", max_length=10)
    include_sections: bool = True
    include_prerequisites: bool = True


class SummaryStartOut(CamelModel):
    run_id: uuid.UUID | None
    started: bool


class SummaryStatusOut(CamelModel):
    provider_available: bool
    status: str | None
    stale: bool
    source_revision: int | None
    current_revision: int
    progress: int
    can_retry: bool


class SummaryArtifactOut(CamelModel):
    artifact_type: str
    title: str | None
    position: int
    content: dict
    source_refs: list[dict]


class SummaryListOut(CamelModel):
    stale: bool
    artifacts: list[SummaryArtifactOut]


@router.post("/{document_id}/summaries", response_model=Envelope[SummaryStartOut], status_code=202)
async def create_summary(
    document_id: uuid.UUID,
    body: SummaryCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    started, run_id = await summary_service.start_summary(
        db,
        doc,
        correlation_id_var.get(),
        learner_level=body.learner_level,
        language=body.language,
        include_sections=body.include_sections,
        include_prerequisites=body.include_prerequisites,
    )
    return wrap(SummaryStartOut(run_id=run_id, started=started))


@router.get("/{document_id}/summaries/status", response_model=Envelope[SummaryStatusOut])
async def summary_status(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    s = await summary_service.get_summary_status(db, doc)
    return wrap(
        SummaryStatusOut(
            provider_available=s.provider_available,
            status=s.status,
            stale=s.stale,
            source_revision=s.source_revision,
            current_revision=s.current_revision,
            progress=s.progress,
            can_retry=s.can_retry,
        )
    )


@router.get("/{document_id}/summaries", response_model=Envelope[SummaryListOut])
async def list_summary(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    artifacts, stale = await summary_service.get_summary_artifacts(db, doc)
    return wrap(
        SummaryListOut(
            stale=stale,
            artifacts=[
                SummaryArtifactOut(
                    artifact_type=a.artifact_type.value,
                    title=a.title,
                    position=a.position,
                    content=a.content_json,
                    source_refs=a.source_refs_json,
                )
                for a in artifacts
            ],
        )
    )


@router.post(
    "/{document_id}/summaries/retry", response_model=Envelope[SummaryStartOut], status_code=202
)
async def retry_summary(
    document_id: uuid.UUID,
    body: SummaryCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    started, run_id = await summary_service.start_summary(
        db,
        doc,
        correlation_id_var.get(),
        learner_level=body.learner_level,
        language=body.language,
        include_sections=body.include_sections,
        include_prerequisites=body.include_prerequisites,
    )
    return wrap(SummaryStartOut(run_id=run_id, started=started))


@router.post("/{document_id}/summaries/cancel", response_model=Envelope[SummaryStartOut])
async def cancel_summary(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    await summary_service.cancel_summary(db, doc)
    return wrap(SummaryStartOut(run_id=None, started=False))


@router.delete("/{document_id}/summaries", response_model=Envelope[SummaryStartOut])
async def delete_summary(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    await summary_service.delete_summaries(db, doc)
    return wrap(SummaryStartOut(run_id=None, started=False))
