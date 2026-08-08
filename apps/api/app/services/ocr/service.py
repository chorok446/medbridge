"""OCR 잡 서비스 — 대상 선택(§7)·디지털 우선 중복 제거·원자적 교체·품질 집계.

디지털 결과는 절대 삭제하지 않는다. OCR 재실행은 OCR 출처 행만 교체한다.
페이지별 ocr_status(pending→terminal)가 진행률·재시작 복구의 영속 기준이다.
"""

import statistics
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
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
from app.models.extraction import DocumentBlock, DocumentLine, DocumentPage
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

    runtime.reject_if_updating()
    if not engine().available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "이 버전에서는 이미지 페이지 읽기를 사용할 수 없습니다.",
            status_code=501,
        )
    if quality not in QUALITY_DPI:
        raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 품질 값입니다.", status_code=422)

    running = await _latest_ocr_job(db, doc.id)
    if running is not None and running.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return doc, 0  # 이미 대기·진행 중 (중복 실행 금지)

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
    try:
        await db.commit()
    except IntegrityError:
        # 활성 잡 유니크 인덱스 — 동시 요청이 이미 시작함(멱등하게 처리)
        await db.rollback()
        return doc, 0
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
    from app.services.tasks.jobs import latest_job

    return await latest_job(db, document_id, JobType.OCR_DOCUMENT)


def classify_result(result: OcrResult) -> tuple[OcrRunStatus, float, float]:
    """반환: (상태, 낮은 신뢰 비율, 중앙값).

    **버려진 단어도 모수에 넣는다.** OCR_MIN_WORD_CONFIDENCE 필터는 OcrResult를
    만들기 전에 걸리므로, `result.words`만 보면 노이즈가 대부분인 쪽이 "남은
    몇 개가 깨끗하니 정상"으로 통과한다 — 100단어 중 85개가 신뢰도 0.1이라
    버려지고 남은 15개가 0.75면 low_ratio가 0이 된다.

    그 판정이 두 곳으로 흘러 같은 방향으로 틀린다: rollup_document_status가 이
    쪽을 unresolved로 세지 않아 문서를 EXTRACTED로 승격시키고, chunking의 저신뢰
    제외에도 걸리지 않아 15단어짜리 잔해가 요약·검색의 근거가 된다. 사용자는
    본문 85%가 통째로 빠진 문서를 '다 읽었다'로 신뢰하고 재시도 안내조차 받지
    못한다. 버려진 단어는 임계값 미만인 것이 확실하므로 저신뢰로 세면 된다.
    """
    if len(result.words) < OCR_EMPTY_MIN_WORDS:
        return OcrRunStatus.OCR_EMPTY, 1.0, 0.0
    confs = [w.confidence for w in result.words]
    dropped = max(0, result.low_quality_dropped)
    low_ratio = (sum(1 for c in confs if c < OCR_LOW_CONFIDENCE_WORD) + dropped) / (
        len(confs) + dropped
    )
    # 중앙값은 살아남은 단어로만 낸다 — 버린 단어의 실제 신뢰도는 이미 없다.
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
    # 블록 metadata로 필터 (JSON 조회는 파이썬에서)
    blocks = (
        await db.execute(select(DocumentBlock).where(DocumentBlock.page_id == page_id))
    ).scalars()
    ocr_ids = [b.id for b in blocks if (b.metadata_json or {}).get("source") == "ocr"]
    if ocr_ids:
        await db.execute(delete(DocumentBlock).where(DocumentBlock.id.in_(ocr_ids)))


async def _digital_normalized_text(db: AsyncSession, page_id: uuid.UUID) -> str:
    """디지털 블록만으로 본문 재구성 — OCR 재실행 시 누적을 막는 단일 기준."""
    rows = (
        await db.execute(
            select(DocumentBlock)
            .where(DocumentBlock.page_id == page_id)
            .order_by(DocumentBlock.reading_order)
        )
    ).scalars()
    texts, excludes = [], []
    for b in rows:
        if (b.metadata_json or {}).get("source") == "ocr":
            continue
        texts.append(b.text or "")
        excludes.append(bool(b.is_header or b.is_footer or b.is_table))
    from app.services.extraction.normalize import page_normalized_text

    return page_normalized_text(texts, exclude_flags=excludes)


async def apply_ocr_result(
    db: AsyncSession, page: DocumentPage, result: OcrResult, run: OcrRun
) -> bool:
    """OCR 결과를 원자적으로 교체 저장 — 같은 트랜잭션에서 커밋은 호출자가 한다.

    반환값은 **페이지 본문이 실제로 달라졌는지**다. 호출자는 이 값으로 문서
    content_revision을 올릴지 정한다 — 같은 결과를 다시 쓴 재실행까지 revision을
    올리면 이미 만들어 둔 청크·요약이 아무 이유 없이 stale이 된다.
    """
    previous_text = page.normalized_text or ""
    # 판정 상태도 함께 본다. 같은 단어를 신뢰도만 다르게 돌려주면 본문은 그대로여도
    # 청크 포함 여부가 뒤집히므로(저신뢰 페이지는 청크에서 빠진다) 재생성이 필요하다.
    #
    # 비교 대상은 `page.ocr_status`가 아니라 **직전 실행 기록**이다. 페이지 상태는 잡을
    # 등록하는 순간 이미 pending으로 덮여 있어, 그걸 기준으로 삼으면 결과가 완전히 같은
    # 재실행도 항상 "바뀜"으로 판정된다.
    previous_status = (
        await db.execute(
            select(OcrRun.status)
            .where(OcrRun.page_id == page.id, OcrRun.id != run.id)
            .order_by(OcrRun.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
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

    max_order, max_block_index = (
        await db.execute(
            select(
                func.max(DocumentBlock.reading_order),
                func.max(DocumentBlock.block_index),
            ).where(DocumentBlock.page_id == page.id)
        )
    ).one()
    max_order = max_order or 0
    max_block_index = max_block_index or 0

    # TSV 계층으로 블록·줄 재구성
    by_block: dict[int, list] = {}
    for w in kept:
        by_block.setdefault(w.block_index, []).append(w)

    block_rows: list[DocumentBlock] = []
    line_rows: list[DocumentLine] = []
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

    db.add_all(block_rows)
    await db.flush()
    db.add_all(line_rows)
    await db.flush()

    status, low_ratio, median = classify_result(result)
    ocr_text = normalize_text("\n".join(b.text for b in block_rows))
    # 재실행 누적 방지: 항상 디지털 기준에서 다시 조립한다
    digital_text = await _digital_normalized_text(db, page.id)
    had_digital = bool(digital_text.strip())
    if ocr_text and had_digital:
        page.normalized_text = f"{digital_text}\n\n{ocr_text}"
        page.extraction_method = "hybrid"
    elif ocr_text:
        page.normalized_text = ocr_text
        page.extraction_method = "ocr"
    else:
        page.normalized_text = digital_text
    page.requires_ocr = False
    page.ocr_status = status.value
    page.ocr_mean_confidence = result.mean_confidence
    # 디지털 기준선은 추출 시점 값 그대로 두고 OCR 몫만 더한다. word_count는 재실행
    # 때마다 덮이므로 여기서 기준선으로 쓸 수 없다.
    page.word_count = page.digital_word_count + len(kept)

    run.status = status
    run.mean_confidence = result.mean_confidence
    run.median_confidence = round(median, 4)
    run.low_confidence_ratio = round(low_ratio, 4)
    run.word_count = len(kept)
    run.duration_ms = result.duration_ms
    run.completed_at = datetime.now(UTC)
    return page.normalized_text != previous_text or status != previous_status


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
    # 실패·빈 결과 페이지는 '읽힌 것'으로 승격하지 않는다 (M6).
    # 저신뢰 결과도 같다 — 단어는 있지만 내용은 노이즈라 청크에서도 제외된다. 이걸
    # '읽힌 것'으로 세면 문서가 extracted로 승격돼, 사용자는 본문 일부가 통째로 빠진
    # 문서를 '다 읽었다'로 신뢰하고 재시도 안내조차 받지 못한다.
    unresolved = sum(
        1
        for p in pages
        if p.extraction_status.value == "failed"
        or p.ocr_status
        in (
            OcrRunStatus.OCR_FAILED.value,
            OcrRunStatus.OCR_EMPTY.value,
            OcrRunStatus.OCR_LOW_CONFIDENCE.value,
        )
    )
    if doc.processing_status not in (
        ProcessingStatus.OCR_REQUIRED,
        ProcessingStatus.PARTIALLY_EXTRACTED,
    ):
        return
    if remaining_ocr == 0 and unresolved == 0:
        transition(doc, ProcessingStatus.EXTRACTED)
    elif doc.processing_status == ProcessingStatus.OCR_REQUIRED and (
        remaining_ocr + unresolved
    ) < len(pages):
        transition(doc, ProcessingStatus.PARTIALLY_EXTRACTED)
