"""요약 백그라운드 작업 — map/reduce 파이프라인 + revision-guarded 원자적 저장.

시작 시점 revision·청크 해시와 저장 직전 값이 같을 때만 artifact를 commit한다.
크래시·중단은 실패로 확정해 사용자가 재실행할 수 있게 한다(OCR/청크와 동일 원칙).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.summary import SummaryArtifact, SummaryRun
from app.models.user import User
from app.services.summary import service as summary_service
from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.executor import (
    SummaryCancelled,
    SummaryNoContent,
    SummaryRevisionChanged,
    execute_hierarchical_summary,
)
from app.services.summary.factory import get_summary_provider

logger = get_logger(__name__)


async def _job_is_current(session, document_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """이 job_id가 여전히 이 문서의 최신 요약 잡이며 활성 상태인지 — 취소·교체 감지."""
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
    if latest is None or latest.id != job_id:
        return False
    job = await session.get(DocumentJob, job_id)
    return job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)


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


def _classify_pipeline_failure(exc: Exception) -> tuple[str, str | None]:
    """내부 예외 원문 없이 DB/API에 저장할 안전한 실패 범주로 축약한다."""
    if isinstance(exc, SummaryNetworkError):
        if exc.category == "timeout":
            return "SUMMARY_TIMEOUT", "timeout"
        if exc.category == "context_overflow":
            # 사용자가 조치할 수 있는 실패(모델 컨텍스트 설정) — 형식 오류와 구분한다.
            return "SUMMARY_CONTEXT_OVERFLOW", "context_overflow"
        if exc.category in ("bad_response", "response_too_large"):
            return "SUMMARY_INVALID_RESPONSE", "invalid_response"
        return "SUMMARY_PROVIDER_ERROR", "provider_error"
    return "SUMMARY_FAILED", None


def _failure_reason(exc: Exception) -> str | None:
    """어떤 계약이 깨졌는지 가리키는 분류값(원문 없음). 로그 진단용."""
    return getattr(exc, "reason", None) if isinstance(exc, SummaryNetworkError) else None


async def run_summary_job(
    document_id: uuid.UUID,
    correlation_id: str,
    *,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    include_sections: bool = True,
    include_prerequisites: bool = True,
) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()

    # 1) 잡·run을 RUNNING으로. 시작 시점 revision·청크 해시 스냅샷은 이미 run에 저장돼 있다.
    #    job_id는 이 run에 대응하는 정확한 잡 — 최신 잡을 다시 고르지 않는다(교차 방지).
    async with factory() as session:
        doc = await session.get(Document, document_id)
        run = await session.get(SummaryRun, run_id)
        job = await session.get(DocumentJob, job_id)
        if doc is None or doc.deleted_at is not None or run is None or job is None:
            return
        if not await _job_is_current(session, document_id, job_id):
            return  # 이미 취소·교체됨
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
            # 전송 직전 동의 재확인 — 시작 후 사용자가 외부 전송을 껐을 수 있다
            if summary_service.provider_is_external(provider):
                doc = await session.get(Document, document_id)
                user = (await session.execute(select(User).limit(1))).scalars().first()
                if doc is None or user is None or not (
                    doc.external_evidence_enabled and user.external_ai_allowed
                ):
                    await _fail_run(session, run_id, job_id, "EXTERNAL_CONSENT_MISSING")
                    return
            chunks = await summary_service.load_chunk_snapshots(session, document_id)

        # 체크포인트 계층 실행 — 노드마다 저장하므로 중단 후 재시도가 성공 노드를 재사용한다.
        drafts = await execute_hierarchical_summary(
            factory,
            document_id=document_id,
            run_id=run_id,
            job_id=job_id,
            provider=provider,
            chunks=chunks,
            learner_level=learner_level,
            language=language,
            include_sections=include_sections,
            include_prerequisites=include_prerequisites,
            start_revision=start_revision,
            start_hash=start_hash,
            job_is_current=_job_is_current,
            current_chunk_hash=summary_service.current_chunk_hash,
        )
    except SummaryCancelled:
        logger.info("summary_job_superseded_or_cancelled", document_id=str(document_id))
        return
    except SummaryRevisionChanged:
        logger.info("summary_revision_changed", document_id=str(document_id))
        async with factory() as session:
            if await _job_is_current(session, document_id, job_id):
                await _fail_run(session, run_id, job_id, "REVISION_CHANGED")
        return
    except SummaryNoContent:
        async with factory() as session:
            if await _job_is_current(session, document_id, job_id):
                await _fail_run(session, run_id, job_id, "SUMMARY_NO_CONTENT")
        return
    except Exception as exc:
        failure_code, failure_category = _classify_pipeline_failure(exc)
        logger.warning(
            "summary_pipeline_failed",
            document_id=str(document_id),
            error_type=type(exc).__name__,
            failure_category=failure_category or "unexpected",
            # 어느 계약이 깨졌는지 — invalid_response 하나에 7가지 원인이 뭉쳐 있어
            # 이 값이 없으면 로그만으로 원인을 좁힐 수 없다.
            failure_reason=_failure_reason(exc) or "none",
        )
        async with factory() as session:
            if await _job_is_current(session, document_id, job_id):
                await _fail_run(session, run_id, job_id, failure_code)
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


async def mark_summary_crashed(document_id: uuid.UUID, *, job_id: uuid.UUID) -> None:
    """크래시 경계 — 이 잡·대응 run을 실패로 확정한다."""
    factory = get_session_factory()
    async with factory() as session:
        job = await session.get(DocumentJob, job_id)
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
