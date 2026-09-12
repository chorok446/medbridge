"""추출 실행·상태·결과 조회 API. 대량 단어 데이터는 페이지 단위로만 로드한다."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import AliasChoices, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var
from app.db.session import get_db
from app.models.enums import PageExtractionStatus, ProcessingStatus
from app.models.extraction import DocumentBlock, DocumentPage, DocumentTable
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.schemas.document import DocumentOut
from app.services.documents.service import get_owned_document
from app.services.extraction.control import cancel_extraction, start_extraction
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["extraction"])


class ExtractionStatusOut(CamelModel):
    processing_status: str
    processing_progress: int
    page_count: int | None
    pages_done: int
    pages_ocr_required: int
    pages_failed: int


class PageSummaryOut(CamelModel):
    page_number: int
    width: float
    height: float
    rotation: int
    extraction_status: str
    scan_verdict: str
    requires_ocr: bool
    reading_order_confidence: float
    text_character_count: int
    table_count: int = 0


class PageDetailOut(PageSummaryOut):
    raw_text: str
    normalized_text: str


class BlockOut(CamelModel):
    id: uuid.UUID
    block_index: int
    block_type: str
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    reading_order: int
    is_header: bool
    is_footer: bool
    is_table: bool
    is_caption: bool


class TableOut(CamelModel):
    id: uuid.UUID
    page_number: int = 0
    table_index: int
    x0: float
    y0: float
    x1: float
    y1: float
    row_count: int
    column_count: int
    # 행 우선 셀 격자(첫 행이 머리글, 빈 칸은 null). 화면이 진짜 <table>로 그리려면
    # 이게 필요하다 — markdown_text만 주면 화면은 `| 파이프 |` 원문을 그대로 내보이게
    # 되고, 그건 DESIGN.md가 이름을 들어 금지한 것이다.
    cells: list[list[str | None]] = Field(
        default_factory=list, validation_alias=AliasChoices("cells", "cells_json")
    )
    markdown_text: str
    confidence: float
    extraction_status: str


async def _get_page(db: AsyncSession, document_id: uuid.UUID, page_number: int) -> DocumentPage:
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
    return page


@router.post("/{document_id}/extract", response_model=Envelope[DocumentOut])
async def extract(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    doc, _ = await start_extraction(db, doc, correlation_id_var.get())
    return wrap(DocumentOut.model_validate(doc))


@router.post("/{document_id}/extract/retry", response_model=Envelope[DocumentOut])
async def extract_retry(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    doc, _ = await start_extraction(db, doc, correlation_id_var.get(), force=True)
    return wrap(DocumentOut.model_validate(doc))


@router.post("/{document_id}/extract/cancel", response_model=Envelope[DocumentOut])
async def extract_cancel(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    doc = await cancel_extraction(db, doc)
    return wrap(DocumentOut.model_validate(doc))


@router.get("/{document_id}/extraction-status", response_model=Envelope[ExtractionStatusOut])
async def extraction_status(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    counts = dict(
        (row[0], row[1])
        for row in (
            await db.execute(
                select(DocumentPage.extraction_status, func.count())
                .where(DocumentPage.document_id == document_id)
                .group_by(DocumentPage.extraction_status)
            )
        ).all()
    )
    return wrap(
        ExtractionStatusOut(
            processing_status=doc.processing_status.value,
            processing_progress=doc.processing_progress,
            page_count=doc.page_count,
            pages_done=sum(counts.values()),
            pages_ocr_required=counts.get(PageExtractionStatus.OCR_REQUIRED, 0),
            pages_failed=counts.get(PageExtractionStatus.FAILED, 0),
        )
    )


@router.get("/{document_id}/pages", response_model=Envelope[list[PageSummaryOut]])
async def list_pages(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    table_counts = dict(
        (row[0], row[1])
        for row in (
            await db.execute(
                select(DocumentTable.page_id, func.count())
                .join(DocumentPage, DocumentPage.id == DocumentTable.page_id)
                .where(DocumentPage.document_id == document_id)
                .group_by(DocumentTable.page_id)
            )
        ).all()
    )
    pages = (
        (
            await db.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_number)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for p in pages:
        item = PageSummaryOut.model_validate(p)
        item.table_count = table_counts.get(p.id, 0)
        out.append(item)
    return wrap(out)


@router.get("/{document_id}/pages/{page_number}", response_model=Envelope[PageDetailOut])
async def page_detail(
    document_id: uuid.UUID,
    page_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    page = await _get_page(db, document_id, page_number)
    return wrap(PageDetailOut.model_validate(page))


@router.get("/{document_id}/pages/{page_number}/blocks", response_model=Envelope[list[BlockOut]])
async def page_blocks(
    document_id: uuid.UUID,
    page_number: int,
    include_bands: bool = Query(default=True, description="머리말·꼬리말 포함 여부"),
    limit: int = Query(default=1000, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    page = await _get_page(db, document_id, page_number)
    stmt = (
        select(DocumentBlock)
        .where(DocumentBlock.page_id == page.id)
        .order_by(DocumentBlock.reading_order)
        .limit(limit)
    )
    blocks = (await db.execute(stmt)).scalars().all()
    if not include_bands:
        blocks = [b for b in blocks if not (b.is_header or b.is_footer)]
    return wrap([BlockOut.model_validate(b) for b in blocks])


@router.get("/{document_id}/tables", response_model=Envelope[list[TableOut]])
async def list_tables(
    document_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_owned_document(db, user, document_id)
    rows = (
        await db.execute(
            select(DocumentTable, DocumentPage.page_number)
            .join(DocumentPage, DocumentPage.id == DocumentTable.page_id)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number, DocumentTable.table_index)
            .limit(limit)
        )
    ).all()
    out = []
    for table, page_number in rows:
        item = TableOut.model_validate(table)
        item.page_number = page_number
        out.append(item)
    return wrap(out)


_ = ProcessingStatus  # (상태 문자열은 DocumentOut을 통해 노출)
