"""청크 재생성 작업 — 문서 전체를 한 번에 재계산하는 단일 배치(페이지별 재개 없음).

크래시 시 재개 지점이 없으므로 실패로 확정한다(OCR stale job 정리와 동일 원칙) —
사용자가 다시 재생성을 요청하면 된다.
"""

import uuid
from datetime import UTC, datetime

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType
from app.services.search.chunking import LowConfidenceOnlyDocument, rebuild_chunks
from app.services.tasks.jobs import latest_job

logger = get_logger(__name__)


async def run_chunk_rebuild_job(document_id: uuid.UUID, correlation_id: str) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()

    async with factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            return
        job = await latest_job(session, document_id, JobType.CHUNK_REBUILD)
        if job is None:
            return
        job_id = job.id
        job.status = JobStatus.RUNNING
        job.attempt_count += 1
        job.started_at = datetime.now(UTC)
        await session.commit()

    try:
        async with factory() as session:
            result = await rebuild_chunks(session, document_id, owner_job_id=job_id)
            await session.commit()
    except LowConfidenceOnlyDocument as exc:
        # 성공으로 마감하면 문서가 '준비됐지만 비어 있는' 상태로 굳고, 화면은 눌러도
        # 소용없는 "문서 검색 준비하기"만 반복해 권하게 된다.
        async with factory() as session:
            job = await session.get(DocumentJob, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.failure_code = "CHUNK_LOW_CONFIDENCE_ONLY"
                job.failure_reason = "low_confidence_only"
                job.completed_at = datetime.now(UTC)
            await session.commit()
        logger.warning(
            "chunk_rebuild_low_confidence_only",
            document_id=str(document_id),
            suppressed_blocks=exc.suppressed_blocks,
        )
        return

    async with factory() as session:
        job = await session.get(DocumentJob, job_id)
        if job is not None:
            job.status = JobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
        await session.commit()
    logger.info(
        "chunk_rebuild_done",
        document_id=str(document_id),
        chunk_count=result.chunk_count,
        # 저신뢰라 청크에서 뺀 블록 수. 0이 아니면 이 문서의 요약·검색은 그만큼을 못 본
        # 채로 완결된 것처럼 보인다 — 청크 수만으로는 그 사실이 드러나지 않아, 실기기
        # 오류 보고서에서 "요약에 이 내용이 왜 없나"를 좁힐 단서가 하나도 없었다.
        suppressed_low_confidence=result.suppressed_low_confidence,
    )


async def mark_chunk_rebuild_crashed(document_id: uuid.UUID) -> None:
    """크래시 경계 — RUNNING/QUEUED로 남은 잡을 실패로 확정해 영구 차단을 막는다."""
    factory = get_session_factory()
    async with factory() as session:
        job = await latest_job(session, document_id, JobType.CHUNK_REBUILD)
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            job.status = JobStatus.FAILED
            job.failure_code = "CHUNK_REBUILD_CRASHED"
            job.completed_at = datetime.now(UTC)
            await session.commit()
