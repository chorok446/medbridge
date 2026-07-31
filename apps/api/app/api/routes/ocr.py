"""OCR 실행·상태·결과 API — 기술 세부(엔진·DPI 등)는 응답에 노출하지 않는다."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var
from app.db.session import get_db
from app.models.document import DocumentJob
from app.models.enums import JobStatus, JobType
from app.models.extraction import DocumentPage
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.schemas.document import DocumentOut
from app.services.documents.service import get_owned_document
from app.services.ocr import service as ocr_service
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["ocr"])


class OcrOptions(CamelModel):
    language: str = Field(default="kor+eng", pattern=r"^[a-z+_]{2,30}$")
    quality: str = Field(default="standard")
    pages: list[int] | None = None


class OcrStartOut(CamelModel):
    target_pages: int
    started: bool


class OcrStatusOut(CamelModel):
    available: bool
    running: bool
    total_targets: int
    done: int
    failed: int
    low_confidence_pages: list[int]
    remaining_ocr_pages: list[int]


class OcrPageResultOut(CamelModel):
    page_number: int
    ocr_status: str | None
    text: str
    quality_notice: bool


@router.post("/{document_id}/ocr", response_model=Envelope[OcrStartOut])
async def start_ocr(
    document_id: uuid.UUID,
    body: OcrOptions | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    options = body or OcrOptions()
    doc, targets = await ocr_service.start_ocr(
        db,
        doc,
        correlation_id_var.get(),
        language=options.language,
        quality=options.quality,
        pages=options.pages,
    )
    return wrap(OcrStartOut(target_pages=targets, started=targets > 0))


@router.post("/{document_id}/ocr/retry", response_model=Envelope[OcrStartOut])
async def retry_ocr(
    document_id: uuid.UUID,
    body: OcrOptions | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """실패·저품질 페이지 재시도 (고품질 모드 기본)."""
    doc = await get_owned_document(db, user, document_id)
    options = body or OcrOptions(quality="high")
    pages = options.pages
    if pages is None:
        rows = (
            await db.execute(
                select(DocumentPage.page_number).where(
                    DocumentPage.document_id == document_id,
                    DocumentPage.ocr_status.in_(
                        ["ocr_failed", "ocr_empty", "ocr_low_confidence", "ocr_cancelled"]
                    ),
                )
            )
        ).all()
        pages = [r[0] for r in rows]
        if not pages:
            raise AppError(
                ErrorCode.INVALID_STATE, "다시 읽을 페이지가 없습니다.", status_code=409
            )
    doc, targets = await ocr_service.start_ocr(
        db,
        doc,
        correlation_id_var.get(),
        language=options.language,
        quality=options.quality or "high",
        pages=pages,
    )
    return wrap(OcrStartOut(target_pages=targets, started=targets > 0))


@router.post("/{document_id}/ocr/cancel", response_model=Envelope[DocumentOut])
async def cancel_ocr(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    doc = await ocr_service.cancel_ocr(db, doc)
    return wrap(DocumentOut.model_validate(doc))


@router.get("/{document_id}/ocr-status", response_model=Envelope[OcrStatusOut])
async def ocr_status(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    pages = (
        await db.execute(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
        )
    ).scalars().all()
    job = (
        await db.execute(
            select(DocumentJob)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.OCR_DOCUMENT,
            )
            .order_by(DocumentJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    in_progress = [p for p in pages if p.ocr_status in ("pending", "running")]
    terminal = [
        p
        for p in pages
        if p.ocr_status
        in ("ocr_completed", "ocr_low_confidence", "ocr_empty", "ocr_failed", "ocr_cancelled")
    ]
    return wrap(
        OcrStatusOut(
            available=ocr_service.engine().available,
            running=job is not None
            and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            and bool(in_progress),
            total_targets=len(in_progress) + len(terminal),
            done=len(terminal),
            failed=sum(1 for p in pages if p.ocr_status == "ocr_failed"),
            low_confidence_pages=[
                p.page_number for p in pages if p.ocr_status == "ocr_low_confidence"
            ],
            remaining_ocr_pages=[p.page_number for p in pages if p.requires_ocr],
        )
    )


@router.post("/{document_id}/pages/{page_number}/ocr", response_model=Envelope[OcrStartOut])
async def ocr_single_page(
    document_id: uuid.UUID,
    page_number: int,
    body: OcrOptions | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    options = body or OcrOptions()
    doc, targets = await ocr_service.start_ocr(
        db,
        doc,
        correlation_id_var.get(),
        language=options.language,
        quality=options.quality,
        pages=[page_number],
    )
    return wrap(OcrStartOut(target_pages=targets, started=targets > 0))


@router.get(
    "/{document_id}/pages/{page_number}/ocr-result",
    response_model=Envelope[OcrPageResultOut],
)
async def ocr_page_result(
    document_id: uuid.UUID,
    page_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    page = (
        await db.execute(
            select(DocumentPage).where(
                DocumentPage.document_id == document_id,
                DocumentPage.page_number == page_number,
            )
        )
    ).scalar_one_or_none()
    if page is None:
        raise AppError(ErrorCode.NOT_FOUND, "페이지를 찾을 수 없습니다.", status_code=404)
    return wrap(
        OcrPageResultOut(
            page_number=page.page_number,
            ocr_status=page.ocr_status,
            text=page.normalized_text,
            quality_notice=page.ocr_status in ("ocr_low_confidence", "ocr_empty"),
        )
    )
