"""요약 서비스 계층 — 시작/상태/조회/재시도/취소/삭제.

동시성은 document_jobs(job_type=summarize) 부분 유니크 인덱스로 DB 레벨 보장.
저장은 revision-guarded commit(시작·완료 시점 revision과 청크 해시가 같을 때만).
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.search import DocumentChunk
from app.models.summary import SummaryArtifact, SummaryNode, SummaryRun
from app.models.user import User
from app.services.summary.factory import get_summary_provider
from app.services.summary.pipeline import ChunkSnapshot
from app.services.summary.provider import provider_checkpoint_fingerprint
from app.services.summary.settings import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    VALID_LEARNER_LEVELS,
)
from app.services.tasks.jobs import latest_job

logger = get_logger(__name__)

# provider 호출 실패와 별개인 앱 중단 자동 복구 상한. 중단을 job.attempt_count에 남기면
# 사용자가 잘못한 실패처럼 재시도 기회를 소모하고, 환급만 하면 crash loop가 무한해진다.
MAX_SUMMARY_AUTO_RESUMES = 3


def compute_chunk_hash(id_hash_pairs: list[tuple[str, str]]) -> str:
    """청크 세트의 안정적 해시 — (chunk_id, content_hash) 쌍 기준.

    chunk_id를 포함해야 한다: 재생성은 동일 텍스트라도 새 UUID·새 source_refs로
    청크를 교체하므로, content_hash만 쓰면 그 교체를 감지하지 못한다(요약이 삭제된
    청크의 출처를 저장하게 됨).
    """
    joined = "\n".join(f"{cid}:{chash}" for cid, chash in sorted(id_hash_pairs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def load_chunk_snapshots(db: AsyncSession, document_id: uuid.UUID) -> list[ChunkSnapshot]:
    rows = (
        (
            await db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    return [
        ChunkSnapshot(
            chunk_id=str(r.id),
            section_title=r.section_title,
            text=r.normalized_text,
            page_start=r.page_start,
            page_end=r.page_end,
            source_refs=list(r.source_refs_json or []),
        )
        for r in rows
    ]


async def current_chunk_hash(db: AsyncSession, document_id: uuid.UUID) -> str:
    rows = (
        await db.execute(
            select(DocumentChunk.id, DocumentChunk.content_hash).where(
                DocumentChunk.document_id == document_id
            )
        )
    ).all()
    return compute_chunk_hash([(str(r.id), r.content_hash) for r in rows])


async def _chunk_count(db: AsyncSession, document_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
    ).scalar_one()


async def _latest_run(db: AsyncSession, document_id: uuid.UUID) -> SummaryRun | None:
    return (
        (
            await db.execute(
                select(SummaryRun)
                .where(SummaryRun.document_id == document_id)
                .order_by(SummaryRun.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_succeeded_run(db: AsyncSession, document_id: uuid.UUID) -> SummaryRun | None:
    return (
        (
            await db.execute(
                select(SummaryRun)
                .where(
                    SummaryRun.document_id == document_id,
                    SummaryRun.status == SummaryRunStatus.SUCCEEDED,
                )
                .order_by(SummaryRun.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _active_summary_job(db: AsyncSession, document_id: uuid.UUID) -> DocumentJob | None:
    return (
        (
            await db.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.SUMMARIZE,
                    DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def provider_is_external(provider) -> bool:
    """네트워크로 문서 내용을 외부에 보내는 공급자인지. 로컬·Disabled·Deterministic은 아니다."""
    return getattr(provider, "provider_name", "") == "openai_compatible" and not getattr(
        provider, "is_local", False
    )


def ensure_external_consent(user: User, doc: Document) -> None:
    """외부 공급자 사용 전 문서·전역 동의를 모두 확인한다."""
    if not (doc.external_evidence_enabled and user.external_ai_allowed):
        raise AppError(
            ErrorCode.INVALID_STATE,
            "외부 요약 모델을 쓰려면 문서와 앱 설정에서 외부 전송을 먼저 허용해 주세요.",
            status_code=403,
        )


async def start_summary(
    db: AsyncSession,
    doc: Document,
    correlation_id: str,
    *,
    user: User,
    learner_level: str,
    language: str,
    include_sections: bool,
    include_prerequisites: bool,
) -> tuple[bool, uuid.UUID | None]:
    """요약 잡 등록. 반환 (started, run_id). 진행 중이면 (False, 기존 run_id)."""
    from app.services.tasks.runner import get_task_runner

    # 재기동 시 동일 provider인지 검증할 수 있도록 시작 시점의 실제 모델 digest와
    # credential identity까지 해석한다. 비밀 원문은 run에 저장하지 않는다.
    provider = await get_summary_provider(db, resolve_identity=True)
    if not provider.available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "요약 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.",
            status_code=501,
        )
    # 외부 공급자는 문서·전역 외부 전송 동의가 모두 있어야 한다
    if provider_is_external(provider):
        ensure_external_consent(user, doc)
    if learner_level not in VALID_LEARNER_LEVELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "잘못된 학습자 수준입니다.", status_code=422)

    # 청크 준비 여부 확인
    if await _chunk_count(db, doc.id) == 0:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "먼저 문서 검색 준비(청크 생성)를 완료해 주세요.",
            status_code=409,
        )
    if doc.chunk_revision != doc.content_revision:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "문서가 변경되어 검색 준비를 다시 해야 합니다. 잠시 후 다시 시도해 주세요.",
            status_code=409,
        )

    existing = await _active_summary_job(db, doc.id)
    if existing is not None:
        run = await _latest_run(db, doc.id)
        return False, run.id if run else None

    source_hash = await current_chunk_hash(db, doc.id)
    job_id = uuid.uuid4()
    job = DocumentJob(
        id=job_id,
        document_id=doc.id,
        job_type=JobType.SUMMARIZE,
        status=JobStatus.QUEUED,
        correlation_id=correlation_id,
    )
    provider_identity_resumable = not bool(getattr(provider, "provider_identity_generation", None))
    provider_fingerprint = provider_checkpoint_fingerprint(
        provider,
        # digest 조회 실패의 실행별 random generation은 다음 factory 해석에서 재현할 수
        # 없다. 초기 task 시작 전 설정 변경 검증에는 안정 config fingerprint를 쓰고,
        # startup 자동 재개는 별도 resumable=False로 금지한다.
        include_fail_safe_generation=provider_identity_resumable,
    )
    run = SummaryRun(
        job_id=job_id,
        document_id=doc.id,
        status=SummaryRunStatus.QUEUED,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        source_revision=doc.content_revision,
        source_chunk_hash=source_hash,
        learner_level=learner_level,
        language=language,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
        # 모델 digest 조회 불가로 일회성 generation만 있는 로컬 공급자는 재기동 뒤
        # 동일성을 증명할 수 없어 resumable=False지만, 최초 실행의 설정 변경은 안정
        # config fingerprint로 계속 차단한다.
        provider_fingerprint=provider_fingerprint,
        provider_identity_resumable=provider_identity_resumable,
    )
    db.add(job)
    db.add(run)
    try:
        await db.commit()
    except IntegrityError:
        # 부분 유니크 인덱스(활성 잡 1개)에 걸림 — 동시 요청이 이미 시작함
        await db.rollback()
        run = await _latest_run(db, doc.id)
        return False, run.id if run else None
    await db.refresh(run)
    await db.refresh(job)
    # 정확히 이 run에 대응하는 job.id를 넘긴다 — 실행기가 최신 잡을 다시 고르면
    # 취소+재시도 사이에서 서로 다른 run/job이 교차될 수 있다.
    get_task_runner().enqueue_summary(
        doc.id,
        correlation_id,
        run_id=run.id,
        job_id=job.id,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
    )
    return True, run.id


@dataclass
class SummaryStatus:
    provider_available: bool
    status: str | None
    stale: bool
    source_revision: int | None
    current_revision: int
    progress: int
    can_retry: bool
    failure_category: str | None
    # 화면에 보이는 요약에 내용이 빠졌는지(성공했지만 일부가 담기지 못함).
    partial: bool = False


@dataclass(frozen=True)
class InterruptedSummaryResume:
    """기동 복구 검증을 통과해 같은 run/job으로 다시 등록할 실행 명세."""

    document_id: uuid.UUID
    run_id: uuid.UUID
    job_id: uuid.UUID
    correlation_id: str
    include_sections: bool
    include_prerequisites: bool


def _fail_interrupted_summary(
    run: SummaryRun,
    job: DocumentJob | None,
    code: str,
    reason: str,
) -> None:
    """복구 불가능한 실행을 비밀·본문 없는 안전한 분류값으로 종료한다."""

    now = datetime.now(UTC)
    run.status = SummaryRunStatus.FAILED
    run.error_code = code
    run.failure_reason = reason
    run.completed_at = now
    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.status = JobStatus.FAILED
        job.failure_code = code
        job.failure_reason = reason
        job.completed_at = now


async def prepare_interrupted_summary_resumes(
    db: AsyncSession,
) -> list[InterruptedSummaryResume]:
    """중단된 요약을 검증하고 재등록 가능한 같은 run/job 목록을 반환한다.

    문서 revision/청크 해시, 저장된 실행 옵션, 공급자 전체 지문, 외부 전송 동의와
    시도 상한을 모두 확인한다. 하나라도 증명할 수 없으면 provider 호출 전에 fail-closed
    종료한다. 이 함수는 설정/keyring/Ollama 모델 카탈로그 외 외부 모델 API를 호출하지 않는다.
    """

    active_runs = list(
        (
            await db.execute(
                select(SummaryRun).where(
                    SummaryRun.status.in_([SummaryRunStatus.QUEUED, SummaryRunStatus.RUNNING])
                )
            )
        ).scalars()
    )
    if not active_runs:
        return []

    resumes: list[InterruptedSummaryResume] = []
    provider = None
    current_fingerprint: str | None = None
    provider_resolved = False
    for run in active_runs:
        job = await db.get(DocumentJob, run.job_id) if run.job_id is not None else None
        if (
            job is None
            or job.document_id != run.document_id
            or job.job_type != JobType.SUMMARIZE
            or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING)
        ):
            _fail_interrupted_summary(
                run,
                (
                    job
                    if job is not None
                    and job.document_id == run.document_id
                    and job.job_type == JobType.SUMMARIZE
                    else None
                ),
                "SUMMARY_RESUME_UNSAFE",
                "resume_job_mismatch",
            )
            continue

        # source/job/attempt처럼 로컬 DB만으로 확인할 수 있는 불변조건을 먼저 검사한다.
        # 이 조건에서 이미 탈락한 실행 때문에 keyring이나 Ollama catalog를 읽지 않는다.
        latest = await latest_job(db, run.document_id, JobType.SUMMARIZE)
        if latest is None or latest.id != job.id:
            _fail_interrupted_summary(run, job, "SUMMARY_RESUME_UNSAFE", "resume_job_superseded")
            continue
        if (run.status == SummaryRunStatus.QUEUED) != (job.status == JobStatus.QUEUED):
            # 두 상태는 같은 트랜잭션에서 함께 전환된다. 서로 다르면 부분 업데이트/손상이라
            # 어느 시도가 실제로 시작됐는지 증명할 수 없으므로 환급하거나 실행하지 않는다.
            _fail_interrupted_summary(run, job, "SUMMARY_RESUME_UNSAFE", "resume_state_mismatch")
            continue
        interrupted_while_running = (
            run.status == SummaryRunStatus.RUNNING and job.status == JobStatus.RUNNING
        )
        effective_attempts = (
            max(0, job.attempt_count - 1) if interrupted_while_running else job.attempt_count
        )
        # 이후 source/provider/동의 검증이 실패해도 앱 중단 자체를 provider 실패로 남기지
        # 않는다. 같은 트랜잭션에서 fail-closed 상태와 함께 commit된다.
        job.attempt_count = effective_attempts
        if effective_attempts >= job.max_attempts:
            _fail_interrupted_summary(
                run,
                job,
                "SUMMARY_ATTEMPTS_EXHAUSTED",
                "resume_attempts_exhausted",
            )
            continue
        if run.resume_count >= MAX_SUMMARY_AUTO_RESUMES:
            _fail_interrupted_summary(
                run,
                job,
                "SUMMARY_RESUME_EXHAUSTED",
                "resume_restart_limit_exhausted",
            )
            continue
        if run.prompt_version != PROMPT_VERSION or run.schema_version != SCHEMA_VERSION:
            # 앱 업데이트로 프롬프트/스키마 계약이 바뀌면 같은 위치의 과거 노드와 새
            # 계획을 섞지 않는다. 새 실행으로 명시 재시도하면 새 run에 새 계약이 저장된다.
            _fail_interrupted_summary(run, job, "SUMMARY_RESUME_UNSAFE", "resume_contract_changed")
            continue

        doc = await db.get(Document, run.document_id)
        if doc is None or doc.deleted_at is not None:
            _fail_interrupted_summary(run, job, "DOCUMENT_GONE", "resume_document_gone")
            continue
        if (
            doc.content_revision != run.source_revision
            or await current_chunk_hash(db, doc.id) != run.source_chunk_hash
        ):
            _fail_interrupted_summary(run, job, "REVISION_CHANGED", "resume_source_changed")
            continue

        if not provider_resolved:
            try:
                provider = await get_summary_provider(db, resolve_identity=True)
            except Exception as exc:
                logger.warning(
                    "summary_resume_provider_resolution_failed", error_type=type(exc).__name__
                )
                provider = None
            current_fingerprint = (
                provider_checkpoint_fingerprint(provider)
                if provider is not None and provider.available
                else None
            )
            provider_resolved = True
        if current_fingerprint is None or provider is None:
            _fail_interrupted_summary(
                run, job, "PROVIDER_UNAVAILABLE", "resume_provider_unavailable"
            )
            continue
        if not run.provider_identity_resumable or run.provider_fingerprint is None:
            _fail_interrupted_summary(
                run, job, "PROVIDER_CHANGED", "resume_provider_identity_unverified"
            )
            continue
        if not hmac.compare_digest(run.provider_fingerprint, current_fingerprint):
            _fail_interrupted_summary(run, job, "PROVIDER_CHANGED", "resume_provider_changed")
            continue
        if provider_is_external(provider):
            user = await db.get(User, doc.user_id)
            if user is None or not (doc.external_evidence_enabled and user.external_ai_allowed):
                _fail_interrupted_summary(
                    run,
                    job,
                    "EXTERNAL_CONSENT_MISSING",
                    "resume_consent_missing",
                )
                continue

        # 중단은 provider/사용자 실패가 아니다. RUNNING은 직전 진입에서 attempt를 이미
        # 올렸으므로 한 번 환급하고, 재개 진입에서 다시 올려 순증가를 0으로 만든다.
        # 대신 영속 resume_count가 반복 crash loop를 독립적으로 제한한다. QUEUED는 아직
        # 진입 전이므로 환급하지 않는다.
        run.resume_count += 1
        run.status = SummaryRunStatus.QUEUED
        run.error_code = None
        run.failure_reason = None
        run.completed_at = None
        job.status = JobStatus.QUEUED
        job.failure_code = None
        job.failure_reason = None
        job.failure_message = None
        job.completed_at = None
        resumes.append(
            InterruptedSummaryResume(
                document_id=run.document_id,
                run_id=run.id,
                job_id=job.id,
                correlation_id=job.correlation_id,
                include_sections=run.include_sections,
                include_prerequisites=run.include_prerequisites,
            )
        )

    await db.commit()
    return resumes


async def cleanup_summary_history(
    db: AsyncSession,
    *,
    retention_days: int = 30,
    max_rows: int = 500,
    batch_size: int = 100,
) -> int:
    """오래된 실패/취소 실행의 자식과 실행 행을 기동당 유계 크기로 정리한다.

    활성 실행과 성공 실행은 후보가 아니다. 자식이 많은 실패 run도 한 트랜잭션에서
    cascade 삭제하지 않고 작은 배치로 나눠 SQLite writer lock과 WAL 증가를 제한한다.
    """

    if retention_days < 1 or max_rows < 1 or batch_size < 1:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    candidate_ids = list(
        (
            await db.execute(
                select(SummaryRun.id)
                .where(
                    SummaryRun.status.in_([SummaryRunStatus.FAILED, SummaryRunStatus.CANCELLED]),
                    func.coalesce(SummaryRun.completed_at, SummaryRun.updated_at) < cutoff,
                )
                .order_by(SummaryRun.created_at)
                .limit(max_rows)
            )
        ).scalars()
    )
    removed = 0
    for run_id in candidate_ids:
        run_complete = True
        for model in (SummaryArtifact, SummaryNode):
            if removed >= max_rows:
                return removed
            ids = list(
                (
                    await db.execute(
                        select(model.id)
                        .where(model.summary_run_id == run_id)
                        .limit(min(batch_size, max_rows - removed))
                    )
                ).scalars()
            )
            if ids:
                await db.execute(delete(model).where(model.id.in_(ids)))
                removed += len(ids)
                await db.commit()
            # 이 run에 자식이 더 남았으면 다음 기동의 작은 배치로 이어서 정리한다.
            more = (
                await db.execute(select(model.id).where(model.summary_run_id == run_id).limit(1))
            ).first()
            if more is not None:
                run_complete = False
                break
        if run_complete:
            if removed < max_rows:
                await db.execute(delete(SummaryRun).where(SummaryRun.id == run_id))
                await db.commit()
                removed += 1
    return removed


def _run_progress(run: SummaryRun | None) -> int:
    """실제 노드 완료 수 기준 진행률. 계층 계획 전에는 상태 기반 근사치를 쓴다.

    대형 문서는 모델 호출이 수백 회라 상태만으로는 진행을 알 수 없다. 계획된 노드 수가
    있으면 완료/계획 비율을 쓰되, 구조화 reduce와 저장이 남아 있으므로 실행 중에는
    99%를 넘기지 않는다.
    """
    if run is None:
        return 0
    if run.status == SummaryRunStatus.SUCCEEDED:
        return 100
    if run.status in (SummaryRunStatus.FAILED, SummaryRunStatus.CANCELLED):
        return 0
    if run.status == SummaryRunStatus.QUEUED:
        return 5
    planned = run.planned_nodes or 0
    if planned <= 0:
        return 10  # RUNNING이지만 아직 그룹 계획 전
    done = min(run.completed_nodes or 0, planned)
    return max(10, min(99, 10 + int(done * 89 / planned)))


async def get_summary_status(db: AsyncSession, doc: Document) -> SummaryStatus:
    provider = await get_summary_provider(db)
    run = await _latest_run(db, doc.id)
    succeeded = await _latest_succeeded_run(db, doc.id)
    stale = bool(succeeded and succeeded.source_revision != doc.content_revision)
    status_val = run.status.value if run else None
    progress = _run_progress(run)
    failure_category: str | None = None
    if run is not None and run.error_code is not None:
        failure_category = {
            "SUMMARY_TIMEOUT": "timeout",
            "SUMMARY_INVALID_RESPONSE": "invalid_response",
            "SUMMARY_CONTEXT_OVERFLOW": "context_overflow",
            "SUMMARY_EMPTY": "empty_result",
            # 성공했지만 컨텍스트 초과로 일부 내용이 빠진 요약. 표시하지 않으면 사용자는
            # 특정 절이 통째로 사라진 요약을 완료된 요약으로 신뢰하게 된다.
            "SUMMARY_PARTIAL": "partial_content",
        }.get(run.error_code)
    # 부분 요약 경고는 **화면에 보이는 artifact를 만든 run** 기준이어야 한다. 최신 run으로
    # 계산하면 이후 재시도가 실패하는 순간 경고만 사라지고(failureCategory가 그 실패로
    # 덮인다) 불완전한 요약은 그대로 남아, 사용자가 그것을 완결된 요약으로 신뢰한다.
    partial = bool(succeeded is not None and succeeded.error_code == "SUMMARY_PARTIAL")
    can_retry = bool(
        provider.available
        and (
            run is None
            or run.status in (SummaryRunStatus.FAILED, SummaryRunStatus.CANCELLED)
            # 내용이 빠진 요약은 메모리 여유가 생기면 더 나은 결과가 나올 수 있다.
            or partial
        )
    )
    return SummaryStatus(
        provider_available=provider.available,
        status=status_val,
        stale=stale,
        source_revision=succeeded.source_revision if succeeded else None,
        current_revision=doc.content_revision,
        progress=progress,
        can_retry=can_retry,
        failure_category=failure_category,
        partial=partial,
    )


async def get_summary_artifacts(
    db: AsyncSession, doc: Document
) -> tuple[list[SummaryArtifact], bool]:
    """최신 성공 run의 artifact 목록 + stale 여부."""
    run = await _latest_succeeded_run(db, doc.id)
    if run is None:
        return [], False
    stale = run.source_revision != doc.content_revision
    artifacts = (
        (
            await db.execute(
                select(SummaryArtifact)
                .where(SummaryArtifact.summary_run_id == run.id)
                .order_by(SummaryArtifact.position)
            )
        )
        .scalars()
        .all()
    )
    return list(artifacts), stale


async def cancel_summary(db: AsyncSession, doc: Document) -> None:
    job = await _active_summary_job(db, doc.id)
    if job is None:
        raise AppError(
            ErrorCode.INVALID_STATE, "지금은 취소할 요약 작업이 없습니다.", status_code=409
        )
    from datetime import UTC

    job.status = JobStatus.FAILED
    job.failure_code = "CANCELLED"
    job.completed_at = datetime.now(UTC)
    run = await _latest_run(db, doc.id)
    if run is not None and run.status in (SummaryRunStatus.QUEUED, SummaryRunStatus.RUNNING):
        run.status = SummaryRunStatus.CANCELLED
        run.error_code = "CANCELLED"
        run.completed_at = datetime.now(UTC)
    await db.commit()


async def delete_summaries(db: AsyncSession, doc: Document) -> None:
    """이 문서의 모든 요약 run·artifact 삭제 (soft-delete 문서라 명시 삭제).

    진행 중 잡이 있으면 먼저 FAILED로 확정한다 — 그러지 않으면 run이 사라진 뒤에도
    잡이 QUEUED로 남아 활성 잡 유니크 인덱스가 이후 모든 요약을 영구 차단한다.
    """
    from datetime import UTC

    job = await _active_summary_job(db, doc.id)
    if job is not None:
        job.status = JobStatus.FAILED
        job.failure_code = "CANCELLED"
        job.completed_at = datetime.now(UTC)
    await db.execute(delete(SummaryArtifact).where(SummaryArtifact.document_id == doc.id))
    # 체크포인트 노드도 함께 지운다 — 남겨두면 삭제 후 재요약이 옛 중간 결과를 재사용한다.
    await db.execute(delete(SummaryNode).where(SummaryNode.document_id == doc.id))
    await db.execute(delete(SummaryRun).where(SummaryRun.document_id == doc.id))
    await db.commit()
