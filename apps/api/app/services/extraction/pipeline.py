"""추출 파이프라인 — 페이지 독립 처리, 원본 좌표 보존, 페이지별 교체 저장.

- 한 페이지의 실패가 문서 전체를 실패시키지 않는다.
- 단일 패스로 페이지를 즉시 저장해 긴 문서에서도 메모리를 페이지 단위로 유지한다.
  머리말·꼬리말은 저장된 블록을 기반으로 후처리 단계에서 표시하고 본문을 재구성한다.
- 실행마다 job_id 토큰을 확인해 취소 직후 재시작된 새 실행과 경합하지 않는다.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.document import Document, DocumentJob
from app.models.enums import (
    BlockType,
    JobType,
    PageExtractionStatus,
    ProcessingStatus,
)
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
from app.services.extraction.headers import _fingerprint, _zone
from app.services.extraction.normalize import (
    is_page_number_line,
    normalize_text,
    page_normalized_text,
)
from app.services.extraction.reading_order import compute_reading_order
from app.services.extraction.scan import classify_page
from app.services.extraction.thresholds import (
    CAPTION_MAX_DISTANCE_PT,
    HEADER_MAX_CHARS,
    REPEAT_MIN_FRACTION,
    REPEAT_MIN_PAGES,
    TABLE_BLOCK_OVERLAP_RATIO,
)

logger = get_logger(__name__)

_CAPTION_RE = re.compile(r"^\s*(그림|표|figure|fig\.?|table)\s*[.\s]?\d", re.IGNORECASE)


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


async def run_is_active(
    session: AsyncSession, document_id: uuid.UUID, job_id: uuid.UUID | None
) -> bool:
    """이 실행이 여전히 유효한지: 문서가 extracting이고, 최신 추출 job이 나 자신일 때만."""
    doc = await session.get(Document, document_id)
    if (
        doc is None
        or doc.deleted_at is not None
        or doc.processing_status != ProcessingStatus.EXTRACTING
    ):
        return False
    if job_id is None:
        return True
    latest = (
        await session.execute(
            select(DocumentJob.id)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.EXTRACT_DOCUMENT,
            )
            .order_by(DocumentJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest == job_id


def _mark_caption_blocks(page: PageData) -> set[int]:
    """이미지·표 근처의 '그림 N/Table N' 블록을 캡션으로 표시."""
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
    return {
        block.block_index
        for block in page.blocks
        if block.block_type == "text"
        and any(overlap_ratio(block.bbox, t) >= TABLE_BLOCK_OVERLAP_RATIO for t in tables)
    }


async def _delete_page_rows(
    session: AsyncSession, document_id: uuid.UUID, page_number: int
) -> None:
    await session.execute(
        delete(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )


async def _write_failed_page(
    session: AsyncSession, document_id: uuid.UUID, page_number: int, error: str
) -> None:
    """실패 페이지 기록 — 재처리 중이었다면 이전 결과도 제거해 정합성을 유지한다."""
    await _delete_page_rows(session, document_id, page_number)
    session.add(
        DocumentPage(
            document_id=document_id,
            page_number=page_number,
            width=0.0,
            height=0.0,
            extraction_status=PageExtractionStatus.FAILED,
            extraction_confidence=0.0,
            error_code="PAGE_EXTRACTION_FAILED",
            error_message_internal=error,
        )
    )


async def _persist_page(
    session: AsyncSession, document_id: uuid.UUID, page: PageData
) -> bool:
    """페이지 결과 저장 (교체 방식). 반환: requires_ocr.

    관계 UUID를 미리 생성해 페이지 전체를 한 번에 add_all 한다 (flush 왕복 최소화).
    """
    await _delete_page_rows(session, document_id, page.page_number)

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

    ordered_blocks = sorted(
        (b for b in page.blocks if b.block_type == "text"),
        key=lambda b: order_pos.get(b.block_index, 10_000),
    )
    normalized = page_normalized_text(
        [b.text for b in ordered_blocks],
        exclude_flags=[b.block_index in table_blocks for b in ordered_blocks],
    )

    page_id = uuid.uuid4()
    page_row = DocumentPage(
            id=page_id,
            document_id=document_id,
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            rotation=page.rotation,
            raw_text=page.raw_text,
            normalized_text=normalized,
            extraction_method=engine_mod.ENGINE_NAME,
            extraction_status=(
                PageExtractionStatus.OCR_REQUIRED
                if scan.requires_ocr
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

    block_rows: list[DocumentBlock] = []
    line_rows: list[DocumentLine] = []
    block_ids: dict[int, uuid.UUID] = {}
    line_ids: dict[tuple[int, int], uuid.UUID] = {}
    for block in page.blocks:
        block_id = uuid.uuid4()
        block_ids[block.block_index] = block_id
        block_rows.append(
            DocumentBlock(
                id=block_id,
                document_id=document_id,
                page_id=page_id,
                block_index=block.block_index,
                block_type=(
                    BlockType.CAPTION
                    if block.block_index in captions
                    else BlockType(block.block_type)
                ),
                x0=block.bbox[0],
                y0=block.bbox[1],
                x1=block.bbox[2],
                y1=block.bbox[3],
                text=block.text,
                reading_order=order_pos.get(block.block_index, block.block_index),
                is_table=block.block_index in table_blocks,
                is_caption=block.block_index in captions,
                confidence=scan.confidence,
                metadata_json={"two_column": order.two_column},
            )
        )
        for line in block.lines:
            line_id = uuid.uuid4()
            line_ids[(block.block_index, line.line_index)] = line_id
            line_rows.append(
                DocumentLine(
                    id=line_id,
                    page_id=page_id,
                    block_id=block_id,
                    line_index=line.line_index,
                    x0=line.bbox[0],
                    y0=line.bbox[1],
                    x1=line.bbox[2],
                    y1=line.bbox[3],
                    text=line.text,
                    reading_order=order_pos.get(block.block_index, block.block_index),
                    metadata_json={},
                )
            )

    tail_rows: list = []
    tail_rows.extend(
        DocumentWord(
            page_id=page_id,
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
    tail_rows.extend(
        DocumentTable(
            page_id=page_id,
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

    # relationship 미정의 모델이므로 FK 의존 순서를 단계별 flush로 보장한다 (페이지당 4회)
    session.add(page_row)
    await session.flush()
    session.add_all(block_rows)
    await session.flush()
    session.add_all(line_rows)
    await session.flush()
    session.add_all(tail_rows)
    return scan.requires_ocr


async def _apply_band_marks(session: AsyncSession, document_id: uuid.UUID) -> None:
    """후처리: 저장된 블록에서 반복 머리말·꼬리말을 찾아 표시하고 본문을 재구성한다."""
    rows = (
        await session.execute(
            select(
                DocumentBlock.id,
                DocumentBlock.block_index,
                DocumentBlock.text,
                DocumentBlock.y0,
                DocumentBlock.y1,
                DocumentBlock.is_table,
                DocumentBlock.reading_order,
                DocumentPage.id.label("page_id"),
                DocumentPage.page_number,
                DocumentPage.height,
            )
            .join(DocumentPage, DocumentPage.id == DocumentBlock.page_id)
            .where(
                DocumentPage.document_id == document_id,
                DocumentBlock.block_type != BlockType.IMAGE,
            )
            .order_by(DocumentPage.page_number, DocumentBlock.reading_order)
        )
    ).all()
    if not rows:
        return

    page_numbers = {r.page_number for r in rows}
    threshold = max(REPEAT_MIN_PAGES, int(len(page_numbers) * REPEAT_MIN_FRACTION))

    from collections import Counter

    counts: Counter[tuple[str, str]] = Counter()
    seen: dict[int, set[tuple[str, str]]] = {}
    for r in rows:
        text = (r.text or "").strip()
        if not text or len(text) > HEADER_MAX_CHARS or r.height <= 0:
            continue
        zone = _zone((0.0, r.y0, 0.0, r.y1), r.height)
        if zone is None:
            continue
        key = (zone, _fingerprint(text))
        if key not in seen.setdefault(r.page_number, set()):
            counts[key] += 1
            seen[r.page_number].add(key)
    repeated = {key for key, n in counts.items() if n >= threshold}

    header_ids: list[uuid.UUID] = []
    footer_ids: list[uuid.UUID] = []
    marked: set[uuid.UUID] = set()
    for r in rows:
        text = (r.text or "").strip()
        if not text or r.height <= 0:
            continue
        zone = _zone((0.0, r.y0, 0.0, r.y1), r.height)
        if zone is None:
            continue
        if (zone, _fingerprint(text)) in repeated or (
            len(text) <= 12 and is_page_number_line(text)
        ):
            (header_ids if zone == "header" else footer_ids).append(r.id)
            marked.add(r.id)

    if header_ids:
        await session.execute(
            update(DocumentBlock).where(DocumentBlock.id.in_(header_ids)).values(is_header=True)
        )
    if footer_ids:
        await session.execute(
            update(DocumentBlock).where(DocumentBlock.id.in_(footer_ids)).values(is_footer=True)
        )
    if not marked:
        return

    # 마킹된 페이지의 본문(normalized_text) 재구성
    affected_pages = {r.page_id for r in rows if r.id in marked}
    by_page: dict[uuid.UUID, list] = {}
    for r in rows:
        by_page.setdefault(r.page_id, []).append(r)
    for page_id in affected_pages:
        page_rows = sorted(by_page[page_id], key=lambda r: r.reading_order)
        text = page_normalized_text(
            [r.text or "" for r in page_rows],
            exclude_flags=[(r.id in marked) or bool(r.is_table) for r in page_rows],
        )
        await session.execute(
            update(DocumentPage).where(DocumentPage.id == page_id).values(normalized_text=text)
        )


async def run_extraction(
    session_factory, document_id: uuid.UUID, job_id: uuid.UUID | None = None
) -> ExtractionSummary | None:
    """문서 전체 추출. 반환 None이면 취소·삭제·실행 교체로 중단된 것."""
    async with session_factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.storage_key is None:
            return ExtractionSummary(0, 0, 0, 0)
        pdf_path = str(storage.get_storage().resolve_path(doc.storage_key))

    fitz_doc = await asyncio.to_thread(engine_mod.open_document, pdf_path)
    try:
        total = fitz_doc.page_count
        summary = ExtractionSummary(total_pages=total, extracted=0, ocr_required=0, failed=0)

        for i in range(total):
            page: PageData | None = None
            error_name = ""
            try:
                page = await asyncio.to_thread(engine_mod.extract_page, fitz_doc, i)
            except Exception as exc:  # 페이지 실패 격리
                logger.warning(
                    "page_extract_failed", document_id=str(document_id), page=i + 1
                )
                error_name = type(exc).__name__

            async with session_factory() as session:
                if not await run_is_active(session, document_id, job_id):
                    return None  # 취소·삭제·새 실행으로 교체됨
                if page is None:
                    await _write_failed_page(session, document_id, i + 1, error_name)
                    summary.failed += 1
                else:
                    try:
                        requires_ocr = await _persist_page(session, document_id, page)
                        if requires_ocr:
                            summary.ocr_required += 1
                        else:
                            summary.extracted += 1
                    except Exception:
                        logger.warning(
                            "page_persist_failed", document_id=str(document_id), page=i + 1
                        )
                        await session.rollback()
                        # 이전 결과가 남지 않도록 실패 페이지로 교체 기록
                        await _write_failed_page(
                            session, document_id, i + 1, "persist_failed"
                        )
                        summary.failed += 1

                doc = await session.get(Document, document_id)
                if doc is not None:
                    doc.processing_progress = int((i + 1) / max(total, 1) * 100)
                await session.commit()

        # 후처리: 머리말·꼬리말 마킹 + 본문 재구성
        async with session_factory() as session:
            if not await run_is_active(session, document_id, job_id):
                return None
            try:
                await _apply_band_marks(session, document_id)
                await session.commit()
            except Exception:
                logger.warning("band_marks_failed", document_id=str(document_id))
                await session.rollback()

        return summary
    finally:
        fitz_doc.close()
