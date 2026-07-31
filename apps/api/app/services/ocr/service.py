"""OCR 잡 서비스 — 대상 선택(§7)·디지털 우선 중복 제거·원자적 교체·품질 집계.

디지털 결과는 절대 삭제하지 않는다. OCR 재실행은 OCR 출처 행만 교체한다.
페이지별 ocr_status(pending→terminal)가 진행률·재시작 복구의 영속 기준이다.
"""

import statistics
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document, DocumentJob
from app.models.enums import (
    BlockType,
    JobStatus,
    JobType,
    OcrRunStatus,
    ProcessingStatus,
)
from app.models.extraction import DocumentBlock, DocumentLine, DocumentPage, DocumentWord
from app.models.ocr import OcrRun
from app.services.documents.state_machine import transition
from app.services.extraction.geometry import overlap_ratio
from app.services.extraction.normalize import normalize_text
from app.services.extraction.ocr import OcrResult, get_ocr_engine
from app.services.ocr.settings import (
    OCR_DEDUPE_OVERLAP_RATIO,
    OCR_EMPTY_MIN_WORDS,
    OCR_LOW_CONFIDENCE_RATIO_WARN,
    OCR_LOW_CONFIDENCE_WORD,
    QUALITY_DPI,
)

logger = get_logger(__name__)

PENDING = "pending"


def engine():
    """테스트에서 monkeypatch 가능한 단일 주입 지점."""
    return get_ocr_engine()


async def select_target_pages(
    db: AsyncSession, document_id: uuid.UUID, pages: list[int] | None
) -> list[DocumentPage]:
    """OCR 대상 페이지(§7): requires_ocr 페이지, 또는 사용자가 지정한 페이지."""
    stmt = select(DocumentPage).where(DocumentPage.document_id == document_id)
    rows = list((await db.execute(stmt)).scalars())
    if pages is not None:
        by_number = {p.page_number: p for p in rows}
        invalid = [n for n in pages if n not in by_number]
        if invalid:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "존재하지 않는 페이지가 포함되어 있습니다.",
                status_code=422,
            )
        return [by_number[n] for n in pages]
    return [p for p in rows if p.requires_ocr]


async def start_ocr(
    db: AsyncSession,
    doc: Document,
    correlation_id: str,
    *,
    language: str = "kor+eng",
    quality: str = "standard",
    pages: list[int] | None = None,
) -> tuple[Document, int]:
    """대상 페이지를 pending으로 표시하고 OCR 잡을 등록한다. 반환: (doc, 대상 수)."""
    from app.services.system import runtime
    from app.services.tasks.runner import get_task_runner

    if runtime.is_updating():
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "업데이트를 준비하는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=503,
            retryable=True,
        )
    if not engine().available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "이 버전에서는 이미지 페이지 읽기를 사용할 수 없습니다.",
            status_code=501,
        )
    if quality not in QUALITY_DPI:
        raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 품질 값입니다.", status_code=422)

    running = await _latest_ocr_job(db, doc.id)
    if running is not None and running.status == JobStatus.RUNNING:
        return doc, 0  # 이미 진행 중 (idempotent)

    targets = await select_target_pages(db, doc.id, pages)
    if not targets:
        return doc, 0

    for page in targets:
        page.ocr_status = PENDING
    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.OCR_DOCUMENT,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
        # 실행 옵션은 correlation 로그로 추적, DPI는 ocr_runs에 기록된다
    )
    db.add(job)
    await db.commit()
    get_task_runner().enqueue_ocr(doc.id, correlation_id, language=language, quality=quality)
    return doc, len(targets)


async def cancel_ocr(db: AsyncSession, doc: Document) -> Document:
    job = await _latest_ocr_job(db, doc.id)
    if job is None or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise AppError(
            ErrorCode.INVALID_STATE, "지금은 취소할 작업이 없습니다.", status_code=409
        )
    job.status = JobStatus.FAILED
    job.failure_code = "CANCELLED"
    job.completed_at = datetime.now(UTC)
    # 남은 대기 페이지 정리
    pending_pages = (
        await db.execute(
            select(DocumentPage).where(
                DocumentPage.document_id == doc.id,
                DocumentPage.ocr_status.in_([PENDING, OcrRunStatus.RUNNING.value]),
            )
        )
    ).scalars()
    for page in pending_pages:
        page.ocr_status = OcrRunStatus.OCR_CANCELLED.value
    await db.commit()
    return doc


async def _latest_ocr_job(db: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    return (
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


def classify_result(result: OcrResult) -> tuple[OcrRunStatus, float, float]:
    """반환: (상태, 낮은 신뢰 비율, 중앙값)."""
    if len(result.words) < OCR_EMPTY_MIN_WORDS:
        return OcrRunStatus.OCR_EMPTY, 1.0, 0.0
    confs = [w.confidence for w in result.words]
    low_ratio = sum(1 for c in confs if c < OCR_LOW_CONFIDENCE_WORD) / len(confs)
    median = statistics.median(confs)
    if low_ratio > OCR_LOW_CONFIDENCE_RATIO_WARN:
        return OcrRunStatus.OCR_LOW_CONFIDENCE, low_ratio, median
    return OcrRunStatus.OCR_COMPLETED, low_ratio, median


async def _digital_text_bboxes(
    db: AsyncSession, page_id: uuid.UUID
) -> list[tuple[float, float, float, float]]:
    rows = (
        await db.execute(
            select(DocumentBlock).where(
                DocumentBlock.page_id == page_id,
                DocumentBlock.block_type != BlockType.IMAGE,
            )
        )
    ).scalars()
    boxes = []
    for b in rows:
        if (b.metadata_json or {}).get("source") != "ocr" and (b.text or "").strip():
            boxes.append((b.x0, b.y0, b.x1, b.y1))
    return boxes


async def _delete_ocr_rows(db: AsyncSession, page_id: uuid.UUID) -> None:
    """OCR 출처 행만 제거 — 디지털 결과는 보존한다."""
    ocr_block_ids = [
        row[0]
        for row in (
            await db.execute(
                select(DocumentBlock.id).where(DocumentBlock.page_id == page_id)
            )
        ).all()
    ]
    # 블록 metadata로 필터 (JSON 조회는 파이썬에서)
    blocks = (
        await db.execute(select(DocumentBlock).where(DocumentBlock.id.in_(ocr_block_ids)))
    ).scalars()
    ocr_ids = [b.id for b in blocks if (b.metadata_json or {}).get("source") == "ocr"]
    if ocr_ids:
        await db.execute(delete(DocumentBlock).where(DocumentBlock.id.in_(ocr_ids)))
    await db.execute(
        delete(DocumentWord).where(
            DocumentWord.page_id == page_id, DocumentWord.source_method == "ocr"
        )
    )


async def apply_ocr_result(
    db: AsyncSession, page: DocumentPage, result: OcrResult, run: OcrRun
) -> None:
    """OCR 결과를 원자적으로 교체 저장 — 같은 트랜잭션에서 커밋은 호출자가 한다."""
    digital_boxes = await _digital_text_bboxes(db, page.id)
    await _delete_ocr_rows(db, page.id)

    # 디지털 우선: 디지털 텍스트와 겹치는 OCR 단어 제외
    kept = [
        w
        for w in result.words
        if not any(
            overlap_ratio(w.bbox, box) >= OCR_DEDUPE_OVERLAP_RATIO for box in digital_boxes
        )
    ]

    max_order = (
        await db.execute(
            select(func.max(DocumentBlock.reading_order)).where(
                DocumentBlock.page_id == page.id
            )
        )
    ).scalar_one_or_none() or 0
    max_block_index = (
        await db.execute(
            select(func.max(DocumentBlock.block_index)).where(
                DocumentBlock.page_id == page.id
            )
        )
    ).scalar_one_or_none() or 0

    # TSV 계층으로 블록·줄 재구성
    by_block: dict[int, list] = {}
    for w in kept:
        by_block.setdefault(w.block_index, []).append(w)

    block_rows: list[DocumentBlock] = []
    line_rows: list[DocumentLine] = []
    word_rows: list[DocumentWord] = []
    order = max_order
    for bi, block_words in sorted(by_block.items()):
        order += 1
        block_id = uuid.uuid4()
        bx0 = min(w.bbox[0] for w in block_words)
        by0 = min(w.bbox[1] for w in block_words)
        bx1 = max(w.bbox[2] for w in block_words)
        by1 = max(w.bbox[3] for w in block_words)
        block_text_lines: dict[tuple[int, int], list] = {}
        for w in block_words:
            block_text_lines.setdefault((w.paragraph_index, w.line_index), []).append(w)
        block_text = "\n".join(
            " ".join(w.text for w in sorted(ws, key=lambda w: w.word_index))
            for _, ws in sorted(block_text_lines.items())
        )
        block_rows.append(
            DocumentBlock(
                id=block_id,
                document_id=page.document_id,
                page_id=page.id,
                block_index=max_block_index + bi + 1,
                block_type=BlockType.TEXT,
                x0=bx0,
                y0=by0,
                x1=bx1,
                y1=by1,
                text=block_text,
                reading_order=order,
                confidence=result.mean_confidence,
                metadata_json={"source": "ocr", "language": result.language},
            )
        )
        for (pi, li), ws in sorted(block_text_lines.items()):
            line_id = uuid.uuid4()
            line_rows.append(
                DocumentLine(
                    id=line_id,
                    page_id=page.id,
                    block_id=block_id,
                    line_index=li,
                    x0=min(w.bbox[0] for w in ws),
                    y0=min(w.bbox[1] for w in ws),
                    x1=max(w.bbox[2] for w in ws),
                    y1=max(w.bbox[3] for w in ws),
                    text=" ".join(w.text for w in sorted(ws, key=lambda w: w.word_index)),
                    reading_order=order,
                    metadata_json={"source": "ocr", "paragraph_index": pi},
                )
            )
            word_rows.extend(
                DocumentWord(
                    page_id=page.id,
                    block_id=block_id,
                    line_id=line_id,
                    word_index=w.word_index,
                    x0=w.bbox[0],
                    y0=w.bbox[1],
                    x1=w.bbox[2],
                    y1=w.bbox[3],
                    text=w.text,
                    normalized_text=normalize_text(w.text),
                    confidence=w.confidence,
                    source_method="ocr",
                    metadata_json={},
                )
                for w in ws
            )

    db.add_all(block_rows)
    await db.flush()
    db.add_all(line_rows)
    await db.flush()
    db.add_all(word_rows)

    status, low_ratio, median = classify_result(result)
    ocr_text = normalize_text(
        "\n".join(b.text for b in block_rows)
    )
    had_digital = bool((page.normalized_text or "").strip())
    if ocr_text:
        page.normalized_text = (
            f"{page.normalized_text}\n\n{ocr_text}" if had_digital else ocr_text
        )
    page.extraction_method = "hybrid" if had_digital and ocr_text else (
        "ocr" if ocr_text else page.extraction_method
    )
    page.requires_ocr = False
    page.ocr_status = status.value
    page.ocr_mean_confidence = result.mean_confidence
    page.word_count = page.word_count + len(word_rows)

    run.status = status
    run.mean_confidence = result.mean_confidence
    run.median_confidence = round(median, 4)
    run.low_confidence_ratio = round(low_ratio, 4)
    run.word_count = len(kept)
    run.duration_ms = result.duration_ms
    run.completed_at = datetime.now(UTC)


async def rollup_document_status(db: AsyncSession, doc: Document) -> None:
    """OCR 후 문서 상태 상향: 남은 requires_ocr·실패 페이지 기준으로 재계산."""
    pages = (
        await db.execute(
            select(DocumentPage).where(DocumentPage.document_id == doc.id)
        )
    ).scalars().all()
    if not pages:
        return
    remaining_ocr = sum(1 for p in pages if p.requires_ocr)
    failed = sum(
        1
        for p in pages
        if p.extraction_status.value == "failed"
        or p.ocr_status in (OcrRunStatus.OCR_FAILED.value,)
    )
    if doc.processing_status not in (
        ProcessingStatus.OCR_REQUIRED,
        ProcessingStatus.PARTIALLY_EXTRACTED,
    ):
        return
    if remaining_ocr == 0 and failed == 0:
        transition(doc, ProcessingStatus.EXTRACTED)
    elif doc.processing_status == ProcessingStatus.OCR_REQUIRED and remaining_ocr < len(pages):
        transition(doc, ProcessingStatus.PARTIALLY_EXTRACTED)
