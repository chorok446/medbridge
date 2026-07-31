"""추출 시작·취소·캐시 판단 (§18: 같은 엔진·스키마로 완료된 문서는 재처리하지 않는다)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, ProcessingStatus
from app.services.documents.state_machine import transition
from app.services.extraction import engine as engine_mod

EXTRACTABLE_STATES = frozenset(
    {
        ProcessingStatus.READY,
        ProcessingStatus.EXTRACTED,
        ProcessingStatus.PARTIALLY_EXTRACTED,
        ProcessingStatus.OCR_REQUIRED,
        ProcessingStatus.EXTRACTION_FAILED,
    }
)


def is_extraction_current(doc: Document) -> bool:
    """완료된 추출이 현재 엔진·스키마와 일치하면 자동 재처리하지 않는다."""
    return (
        doc.processing_status == ProcessingStatus.EXTRACTED
        and doc.extraction_engine == engine_mod.ENGINE_NAME
        and doc.extraction_engine_version == engine_mod.ENGINE_VERSION
        and doc.extraction_schema_version == engine_mod.SCHEMA_VERSION
    )


async def start_extraction(
    db: AsyncSession,
    doc: Document,
    correlation_id: str,
    *,
    force: bool = False,
) -> tuple[Document, bool]:
    """반환: (doc, started). 이미 최신 결과가 있고 force가 아니면 시작하지 않는다."""
    from app.services.system import runtime
    from app.services.tasks.runner import get_task_runner

    if runtime.is_updating():
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "업데이트를 준비하는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=503,
            retryable=True,
        )
    if doc.processing_status == ProcessingStatus.EXTRACTING:
        return doc, False  # 이미 진행 중 (idempotent)
    if doc.processing_status not in EXTRACTABLE_STATES:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "아직 파일 확인이 끝나지 않아 내용을 읽을 수 없습니다.",
            status_code=409,
        )
    if not force and is_extraction_current(doc):
        return doc, False

    transition(doc, ProcessingStatus.EXTRACTING)
    doc.processing_progress = 0
    doc.failure_code = None
    doc.failure_message = None
    job = DocumentJob(
        document_id=doc.id,
        job_type=JobType.EXTRACT_DOCUMENT,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    db.add(job)
    await db.commit()
    get_task_runner().enqueue_extract(doc.id, correlation_id)
    return doc, True


async def cancel_extraction(db: AsyncSession, doc: Document) -> Document:
    """진행 중 추출 취소 — 파이프라인은 상태 변화를 감지하고 페이지 경계에서 멈춘다."""
    if doc.processing_status != ProcessingStatus.EXTRACTING:
        raise AppError(
            ErrorCode.INVALID_STATE, "지금은 취소할 작업이 없습니다.", status_code=409
        )
    transition(doc, ProcessingStatus.READY)
    doc.processing_progress = 0
    await db.commit()
    return doc


def make_correlation_id() -> str:
    return f"extract-{uuid.uuid4().hex[:10]}"
