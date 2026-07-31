"""요약 백그라운드 작업 — map/reduce 파이프라인 + revision-guarded 원자적 저장.

시작 시점 revision·청크 해시와 저장 직전 값이 같을 때만 artifact를 commit한다.
크래시·중단은 실패로 확정해 사용자가 재실행할 수 있게 한다(OCR/청크와 동일 원칙).
"""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.summary import SummaryArtifact, SummaryRun
from app.services.summary import service as summary_service
from app.services.summary.factory import get_summary_provider
from app.services.summary.pipeline import run_summary

logger = get_logger(__name__)


async def _job_is_current(session, document_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    latest = (
        await session.execute(
            select(DocumentJob)
            .where(
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == JobType.SUMMARIZE,
            )
            .order_by(DocumentJob.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return (
        latest is not None
        and latest.id == job_id
        and latest.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    )


async def _fail_run(session, run_id: uuid.UUID, job_id: uuid.UUID, code: str) -> None:
    run = await session.get(SummaryRun, run_id)
    if run is not None and run.status in (SummaryRunStatus.QUEUED, SummaryRunStatus.RUNNING):
        run.status = SummaryRunStatus.FAILED
        run.error_code = code
        run.completed_at = datetime.now(UTC)
    job = await session.get(DocumentJob, job_id)
    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.status = JobStatus.FAILED
        job.failure_code = code
        job.completed_at = datetime.now(UTC)
    await session.commit()


async def run_summary_job(
    document_id: uuid.UUID,
    correlation_id: str,
    *,
    run_id: uuid.UUID,
    include_sections: bool = True,
    include_prerequisites: bool = True,
) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()

    # 1) 잡·run을 RUNNING으로. 시작 시점 revision·청크 해시 스냅샷은 이미 run에 저장돼 있다.
    async with factory() as session:
        doc = await session.get(Document, document_id)
        run = await session.get(SummaryRun, run_id)
        if doc is None or doc.deleted_at is not None or run is None:
            return
        job = (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.SUMMARIZE,
                )
                .order_by(DocumentJob.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if job is None:
            return
        job_id = job.id
        job.status = JobStatus.RUNNING
        job.attempt_count += 1
        job.started_at = datetime.now(UTC)
        run.status = SummaryRunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        start_revision = run.source_revision
        start_hash = run.source_chunk_hash
        learner_level = run.learner_level
        language = run.language
        await session.commit()

    # 2) 청크 스냅샷 + 공급자 해석 → 파이프라인(네트워크 I/O 가능하므로 스레드에서)
    try:
        async with factory() as session:
            provider = await get_summary_provider(session)
            if not provider.available:
                await _fail_run(session, run_id, job_id, "PROVIDER_UNAVAILABLE")
                return
            chunks = await summary_service.load_chunk_snapshots(session, document_id)

        drafts = await asyncio.to_thread(
            run_summary,
            provider,
            chunks,
            learner_level=learner_level,
            language=language,
            include_sections=include_sections,
            include_prerequisites=include_prerequisites,
        )
    except Exception as exc:
        logger.warning(
            "summary_pipeline_failed",
            document_id=str(document_id),
            error=type(exc).__name__,
        )
        async with factory() as session:
            if await _job_is_current(session, document_id, job_id):
                await _fail_run(session, run_id, job_id, "SUMMARY_FAILED")
        return

    # 3) revision-guarded 원자적 저장
    async with factory() as session:
        if not await _job_is_current(session, document_id, job_id):
            logger.info("summary_job_superseded_or_cancelled", document_id=str(document_id))
            return
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            await _fail_run(session, run_id, job_id, "DOCUMENT_GONE")
            return
        current_hash = await summary_service.current_chunk_hash(session, document_id)
        if doc.content_revision != start_revision or current_hash != start_hash:
            # 요약 도중 문서가 바뀜 — 결과를 저장하지 않는다
            logger.info("summary_revision_changed", document_id=str(document_id))
            await _fail_run(session, run_id, job_id, "REVISION_CHANGED")
            return

        for draft in drafts:
            session.add(
                SummaryArtifact(
                    document_id=document_id,
                    summary_run_id=run_id,
                    artifact_type=draft.artifact_type,
                    title=draft.title,
                    position=draft.position,
                    content_json=draft.content_json,
                    source_chunk_ids_json=draft.source_chunk_ids,
                    source_refs_json=draft.source_refs,
                )
            )
        run = await session.get(SummaryRun, run_id)
        if run is not None:
            run.status = SummaryRunStatus.SUCCEEDED
            run.completed_at = datetime.now(UTC)
        job = await session.get(DocumentJob, job_id)
        if job is not None:
            job.status = JobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
        await session.commit()
    logger.info(
        "summary_job_done", document_id=str(document_id), artifacts=len(drafts)
    )


async def mark_summary_crashed(document_id: uuid.UUID) -> None:
    """크래시 경계 — RUNNING/QUEUED 요약 잡·run을 실패로 확정한다."""
    factory = get_session_factory()
    async with factory() as session:
        job = (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.SUMMARIZE,
                )
                .order_by(DocumentJob.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            job.status = JobStatus.FAILED
            job.failure_code = "SUMMARY_CRASHED"
            job.completed_at = datetime.now(UTC)
        run = await summary_service._latest_run(session, document_id)
        if run is not None and run.status in (
            SummaryRunStatus.QUEUED,
            SummaryRunStatus.RUNNING,
        ):
            run.status = SummaryRunStatus.FAILED
            run.error_code = "SUMMARY_CRASHED"
            run.completed_at = datetime.now(UTC)
        await session.commit()
