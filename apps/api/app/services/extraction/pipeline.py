"""추출 파이프라인 — 페이지 독립 처리, 원본 좌표 보존, 페이지별 교체 저장.

한 페이지의 실패가 문서 전체를 실패시키지 않는다.
재처리 시 페이지 단위로 기존 결과를 지우고 새 결과를 넣으므로 중단·재실행에 안전하다.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.document import Document
from app.models.enums import BlockType, PageExtractionStatus, ProcessingStatus
from app.models.extraction import (
    DocumentBlock,
    DocumentLine,
    DocumentPage,
    DocumentTable,
    DocumentWord,
)
from app.services.documents import storage
from app.services.extraction import engine as engine_mod
from app.services.extraction.engine import PageData
from app.services.extraction.geometry import overlap_ratio, vertical_distance
from app.services.extraction.headers import detect_repeated_bands
from app.services.extraction.normalize import normalize_text, page_normalized_text
from app.services.extraction.reading_order import compute_reading_order
from app.services.extraction.scan import classify_page
from app.services.extraction.thresholds import (
    CAPTION_MAX_DISTANCE_PT,
    TABLE_BLOCK_OVERLAP_RATIO,
)

logger = get_logger(__name__)

_CAPTION_RE = re.compile(r"^\s*(그림|표|figure|fig\.?|table)\s*[.\s]?\d", re.IGNORECASE)


@dataclass
class PageOutcome:
    page_number: int
    status: PageExtractionStatus
    requires_ocr: bool


@dataclass
class ExtractionSummary:
    total_pages: int
    extracted: int
    ocr_required: int
    failed: int

    @property
    def final_status(self) -> ProcessingStatus:
        if self.total_pages == 0 or self.extracted + self.ocr_required + self.failed == 0:
            return ProcessingStatus.EXTRACTION_FAILED
        if self.failed == 0 and self.ocr_required == 0:
            return ProcessingStatus.EXTRACTED
        if self.extracted == 0 and self.ocr_required > 0 and self.failed == 0:
            return ProcessingStatus.OCR_REQUIRED
        if self.extracted == 0 and self.ocr_required == 0:
            return ProcessingStatus.EXTRACTION_FAILED
        return ProcessingStatus.PARTIALLY_EXTRACTED


async def _still_extracting(session: AsyncSession, document_id: uuid.UUID) -> bool:
    doc = await session.get(Document, document_id)
    return (
        doc is not None
        and doc.deleted_at is None
        and doc.processing_status == ProcessingStatus.EXTRACTING
    )


def _mark_caption_blocks(page: PageData) -> set[int]:
    """이미지·표 근처의 짧은 '그림 N/Table N' 블록을 캡션으로 표시."""
    anchors = list(page.image_bboxes) + [t.bbox for t in page.tables]
    captions: set[int] = set()
    if not anchors:
        return captions
    for block in page.blocks:
        if block.block_type != "text" or not _CAPTION_RE.match(block.text or ""):
            continue
        if any(vertical_distance(block.bbox, a) <= CAPTION_MAX_DISTANCE_PT for a in anchors):
            captions.add(block.block_index)
    return captions


def _mark_table_blocks(page: PageData) -> set[int]:
    tables = [t.bbox for t in page.tables]
    marked: set[int] = set()
    for block in page.blocks:
        if block.block_type != "text":
            continue
        if any(overlap_ratio(block.bbox, t) >= TABLE_BLOCK_OVERLAP_RATIO for t in tables):
            marked.add(block.block_index)
    return marked


async def _persist_page(
    session: AsyncSession,
    document_id: uuid.UUID,
    page: PageData,
    band_marks: dict[int, str],
) -> PageOutcome:
    # 기존 페이지 결과 교체 (CASCADE로 blocks/lines/words/tables 정리)
    await session.execute(
        delete(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page.page_number,
        )
    )

    order = compute_reading_order(page.blocks, page.width, page.height)
    order_pos = {block_index: pos for pos, block_index in enumerate(order.order)}
    captions = _mark_caption_blocks(page)
    table_blocks = _mark_table_blocks(page)

    char_count = len("".join(page.raw_text.split()))
    scan = classify_page(
        char_count=char_count,
        word_count=len(page.words),
        image_area_ratio=page.image_area_ratio,
        full_page_image=page.full_page_image,
        has_text_blocks=any(b.block_type == "text" and b.text.strip() for b in page.blocks),
        raw_text=page.raw_text,
    )

    # 본문(정규화) 텍스트: 읽기 순서대로, 머리말·꼬리말·표 내부 중복 제외
    ordered_blocks = sorted(
        (b for b in page.blocks if b.block_type == "text"),
        key=lambda b: order_pos.get(b.block_index, 10_000),
    )
    normalized = page_normalized_text(
        [b.text for b in ordered_blocks],
        exclude_flags=[
            b.block_index in band_marks or b.block_index in table_blocks
            for b in ordered_blocks
        ],
    )

    page_row = DocumentPage(
        document_id=document_id,
        page_number=page.page_number,
        width=page.width,
        height=page.height,
        rotation=page.rotation,
        raw_text=page.raw_text,
        normalized_text=normalized,
        extraction_method=engine_mod.ENGINE_NAME,
        extraction_status=(
            PageExtractionStatus.OCR_REQUIRED if scan.requires_ocr
            else PageExtractionStatus.EXTRACTED
        ),
        extraction_confidence=scan.confidence,
        reading_order_confidence=order.confidence,
        scan_verdict=scan.verdict,
        text_character_count=char_count,
        word_count=len(page.words),
        image_area_ratio=round(page.image_area_ratio, 4),
        requires_ocr=scan.requires_ocr,
    )
    session.add(page_row)
    await session.flush()

    block_ids: dict[int, uuid.UUID] = {}
    line_ids: dict[tuple[int, int], uuid.UUID] = {}
    for block in page.blocks:
        band = band_marks.get(block.block_index)
        block_row = DocumentBlock(
            document_id=document_id,
            page_id=page_row.id,
            block_index=block.block_index,
            block_type=(
                BlockType.CAPTION if block.block_index in captions
                else BlockType(block.block_type)
            ),
            x0=block.bbox[0],
            y0=block.bbox[1],
            x1=block.bbox[2],
            y1=block.bbox[3],
            text=block.text,
            reading_order=order_pos.get(block.block_index, block.block_index),
            is_header=band == "header",
            is_footer=band == "footer",
            is_table=block.block_index in table_blocks,
            is_caption=block.block_index in captions,
            confidence=scan.confidence,
            metadata_json={"two_column": order.two_column},
        )
        session.add(block_row)
        await session.flush()
        block_ids[block.block_index] = block_row.id
        for line in block.lines:
            line_row = DocumentLine(
                page_id=page_row.id,
                block_id=block_row.id,
                line_index=line.line_index,
                x0=line.bbox[0],
                y0=line.bbox[1],
                x1=line.bbox[2],
                y1=line.bbox[3],
                text=line.text,
                reading_order=order_pos.get(block.block_index, block.block_index),
                metadata_json={},
            )
            session.add(line_row)
            await session.flush()
            line_ids[(block.block_index, line.line_index)] = line_row.id

    session.add_all(
        DocumentWord(
            page_id=page_row.id,
            block_id=block_ids.get(word.block_index),
            line_id=line_ids.get((word.block_index, word.line_index)),
            word_index=word.word_index,
            x0=word.bbox[0],
            y0=word.bbox[1],
            x1=word.bbox[2],
            y1=word.bbox[3],
            text=word.text,
            normalized_text=normalize_text(word.text),
            confidence=1.0,
            metadata_json={},
        )
        for word in page.words
    )

    session.add_all(
        DocumentTable(
            page_id=page_row.id,
            table_index=i,
            x0=t.bbox[0],
            y0=t.bbox[1],
            x1=t.bbox[2],
            y1=t.bbox[3],
            row_count=t.row_count,
            column_count=t.column_count,
            cells_json=t.cells,
            markdown_text=t.markdown,
            confidence=t.confidence,
            extraction_status=t.status,
            metadata_json={},
        )
        for i, t in enumerate(page.tables)
    )

    return PageOutcome(
        page_number=page.page_number,
        status=page_row.extraction_status,
        requires_ocr=scan.requires_ocr,
    )


async def run_extraction(
    session_factory, document_id: uuid.UUID
) -> ExtractionSummary | None:
    """문서 전체 추출. 반환 None이면 취소·삭제로 중단된 것."""
    async with session_factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.storage_key is None:
            return ExtractionSummary(0, 0, 0, 0)
        pdf_path = str(storage.get_storage().resolve_path(doc.storage_key))

    fitz_doc = await asyncio.to_thread(engine_mod.open_document, pdf_path)
    try:
        total = fitz_doc.page_count

        # 1차: 페이지 구조 추출 (머리말·꼬리말 판정은 문서 전체가 필요)
        pages: list[PageData | Exception] = []
        for i in range(total):
            async with session_factory() as session:
                if not await _still_extracting(session, document_id):
                    return None  # 취소 또는 삭제
            try:
                pages.append(await asyncio.to_thread(engine_mod.extract_page, fitz_doc, i))
            except Exception as exc:  # 페이지 실패 격리
                logger.warning(
                    "page_extract_failed", document_id=str(document_id), page=i + 1
                )
                pages.append(exc)

        band_marks = detect_repeated_bands([p for p in pages if isinstance(p, PageData)])

        # 2차: 페이지별 저장 (페이지 단위 커밋 → 진행률·중단 안전)
        summary = ExtractionSummary(total_pages=total, extracted=0, ocr_required=0, failed=0)
        for i, page in enumerate(pages):
            async with session_factory() as session:
                if not await _still_extracting(session, document_id):
                    return None
                if isinstance(page, Exception):
                    await session.execute(
                        delete(DocumentPage).where(
                            DocumentPage.document_id == document_id,
                            DocumentPage.page_number == i + 1,
                        )
                    )
                    session.add(
                        DocumentPage(
                            document_id=document_id,
                            page_number=i + 1,
                            width=0.0,
                            height=0.0,
                            extraction_status=PageExtractionStatus.FAILED,
                            extraction_confidence=0.0,
                            error_code="PAGE_EXTRACTION_FAILED",
                            error_message_internal=type(page).__name__,
                        )
                    )
                    summary.failed += 1
                else:
                    try:
                        outcome = await _persist_page(
                            session, document_id, page, band_marks.get(page.page_number, {})
                        )
                        if outcome.requires_ocr:
                            summary.ocr_required += 1
                        else:
                            summary.extracted += 1
                    except Exception:
                        logger.warning(
                            "page_persist_failed",
                            document_id=str(document_id),
                            page=i + 1,
                        )
                        await session.rollback()
                        summary.failed += 1

                doc = await session.get(Document, document_id)
                if doc is not None:
                    doc.processing_progress = int((i + 1) / max(total, 1) * 100)
                await session.commit()

        return summary
    finally:
        fitz_doc.close()
