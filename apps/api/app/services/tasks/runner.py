"""로컬 작업 실행기 — Redis/Dramatiq를 대체하는 인프로세스 비동기 실행.

- TaskRunner 인터페이스 뒤에서 asyncio 태스크로 실행, 동시 실행 수 제한
- 작업 상태는 SQLite document_jobs에 저장 (앱 종료 시에도 상태 보존)
- 앱 재시작 시 queued/validating에 멈춘 문서를 복구한다
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.document import Document
from app.models.enums import ProcessingStatus

logger = get_logger(__name__)


class TaskRunner(Protocol):
    def enqueue_validate(self, document_id: uuid.UUID, correlation_id: str) -> None: ...


class LocalTaskRunner:
    def __init__(self, max_concurrent: int | None = None) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent or get_settings().max_concurrent_jobs)
        self._tasks: set[asyncio.Task] = set()

    def enqueue_validate(self, document_id: uuid.UUID, correlation_id: str) -> None:
        self._spawn(self._run_validate(document_id, correlation_id))

    def enqueue_extract(self, document_id: uuid.UUID, correlation_id: str) -> None:
        self._spawn(self._run_extract(document_id, correlation_id))

    def enqueue_ocr(
        self,
        document_id: uuid.UUID,
        correlation_id: str,
        *,
        language: str = "kor+eng",
        quality: str = "standard",
    ) -> None:
        self._spawn(self._run_ocr(document_id, correlation_id, language, quality))

    def enqueue_chunk_rebuild(self, document_id: uuid.UUID, correlation_id: str) -> None:
        self._spawn(self._run_chunk_rebuild(document_id, correlation_id))

    def enqueue_summary(
        self,
        document_id: uuid.UUID,
        correlation_id: str,
        *,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        include_sections: bool = True,
        include_prerequisites: bool = True,
    ) -> None:
        self._spawn(
            self._run_summary(
                document_id,
                correlation_id,
                run_id,
                job_id,
                include_sections,
                include_prerequisites,
            )
        )

    async def _run_guarded(
        self,
        job_name: str,
        document_id: uuid.UUID,
        run: Callable[[], Awaitable[None]],
        mark_crashed: Callable[[], Awaitable[None]],
    ) -> None:
        """세마포어 → 실행 → 크래시 로그 → 실패 확정 — 모든 잡 유형이 공유하는 가드.

        크래시 시 문서가 진행 중 상태에 고착되지 않도록 실패로 확정한다. 로그 이벤트
        이름은 잡 유형과 무관하게 고정해 오류 보고서에서 grep 하나로 찾을 수 있게 한다.
        """
        async with self._semaphore:
            try:
                await run()
            except Exception:
                logger.error("task_crashed", job=job_name, document_id=str(document_id))
                try:
                    await mark_crashed()
                except Exception:
                    logger.error("mark_crashed_failed", job=job_name, document_id=str(document_id))

    async def _run_summary(
        self,
        document_id: uuid.UUID,
        correlation_id: str,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        include_sections: bool,
        include_prerequisites: bool,
    ) -> None:
        from app.services.tasks.summary_job import mark_summary_crashed, run_summary_job

        await self._run_guarded(
            "summary",
            document_id,
            lambda: run_summary_job(
                document_id,
                correlation_id,
                run_id=run_id,
                job_id=job_id,
                include_sections=include_sections,
                include_prerequisites=include_prerequisites,
            ),
            lambda: mark_summary_crashed(document_id, job_id=job_id),
        )

    async def _run_chunk_rebuild(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.chunk_job import (
            mark_chunk_rebuild_crashed,
            run_chunk_rebuild_job,
        )

        await self._run_guarded(
            "chunk_rebuild",
            document_id,
            lambda: run_chunk_rebuild_job(document_id, correlation_id),
            lambda: mark_chunk_rebuild_crashed(document_id),
        )

    async def _run_ocr(
        self, document_id: uuid.UUID, correlation_id: str, language: str, quality: str
    ) -> None:
        from app.services.tasks.ocr_job import mark_ocr_job_crashed, run_ocr_job

        await self._run_guarded(
            "ocr",
            document_id,
            lambda: run_ocr_job(document_id, correlation_id, language=language, quality=quality),
            lambda: mark_ocr_job_crashed(document_id),
        )

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_extract(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.extract import extract_document, mark_extraction_crashed

        await self._run_guarded(
            "extract",
            document_id,
            lambda: extract_document(document_id, correlation_id),
            lambda: mark_extraction_crashed(document_id),
        )

    async def _run_validate(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.validate import mark_validation_crashed, validate_document

        await self._run_guarded(
            "validate",
            document_id,
            lambda: validate_document(document_id, correlation_id),
            lambda: mark_validation_crashed(document_id),
        )

    async def recover_interrupted(self) -> int:
        """앱 재시작 시 중단 작업 복구.

        - queued/validating/uploaded: 다시 검증 큐로 (uploaded는 큐 등록 전에 중단된 경우)
        - created/uploading: 파일 저장이 보장되지 않으므로 실패로 확정 (재업로드 안내)
        """
        from app.services.tasks.extract import (
            _latest_job,
            mark_extraction_attempts_exhausted,
        )

        to_enqueue: list[uuid.UUID] = []
        to_extract: list[uuid.UUID] = []
        interrupted_uploads: list[uuid.UUID] = []
        async with get_session_factory()() as session:
            stmt = select(Document).where(
                Document.processing_status.in_(
                    [
                        ProcessingStatus.CREATED,
                        ProcessingStatus.UPLOADING,
                        ProcessingStatus.UPLOADED,
                        ProcessingStatus.QUEUED,
                        ProcessingStatus.VALIDATING,
                        ProcessingStatus.EXTRACTING,
                    ]
                ),
                Document.deleted_at.is_(None),
            )
            docs = list((await session.execute(stmt)).scalars())
            for doc in docs:
                if doc.processing_status in (
                    ProcessingStatus.CREATED,
                    ProcessingStatus.UPLOADING,
                ):
                    interrupted_uploads.append(doc.id)
                    doc.processing_status = ProcessingStatus.FAILED  # 내부 복구 경로
                    doc.storage_key = None
                    doc.failure_code = "VALIDATION_FAILED"
                    doc.failure_message = "업로드가 중단되었습니다. 파일을 다시 업로드해 주세요."
                elif doc.processing_status == ProcessingStatus.EXTRACTING:
                    job = await _latest_job(session, doc.id)
                    if job is not None and job.attempt_count >= job.max_attempts:
                        mark_extraction_attempts_exhausted(doc, job)
                    else:
                        # 페이지 단위 교체 저장이라 허용 횟수 안에서는 재실행이 안전하다.
                        to_extract.append(doc.id)
                else:
                    if doc.processing_status != ProcessingStatus.QUEUED:
                        doc.processing_status = ProcessingStatus.QUEUED  # 내부 복구 경로
                    to_enqueue.append(doc.id)
            await session.commit()
        # 삭제 실패로 orphan이 남아도 다음 기동 때 다시 시도한다. storage_key가 없는
        # failed 행은 정상 파일을 가리킬 수 없으므로 UUID 경로 삭제가 안전한 marker다.
        async with get_session_factory()() as session:
            failed_without_storage = list(
                (
                    await session.execute(
                        select(Document.id).where(
                            Document.processing_status == ProcessingStatus.FAILED,
                            Document.storage_key.is_(None),
                        )
                    )
                ).scalars()
            )
        interrupted_uploads.extend(
            doc_id for doc_id in failed_without_storage if doc_id not in interrupted_uploads
        )
        if interrupted_uploads:
            from app.services.documents import storage

            for doc_id in interrupted_uploads:
                try:
                    await asyncio.to_thread(storage.delete_original, storage.object_key(doc_id))
                except Exception:
                    logger.warning("interrupted_upload_cleanup_failed", document_id=str(doc_id))
        for doc_id in to_enqueue:
            self.enqueue_validate(doc_id, f"recovery-{uuid.uuid4().hex[:8]}")
        for doc_id in to_extract:
            self.enqueue_extract(doc_id, f"recovery-{uuid.uuid4().hex[:8]}")
        to_enqueue.extend(to_extract)

        # 중단된 OCR 복구: pending/running 페이지가 남은 문서를 다시 실행
        from app.models.extraction import DocumentPage
        from app.models.ocr import OcrRun
        from app.services.ocr.settings import QUALITY_DPI

        dpi_to_quality = {dpi: quality for quality, dpi in QUALITY_DPI.items()}
        ocr_options: dict[uuid.UUID, tuple[str, str]] = {}
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentPage.document_id, DocumentPage.id, DocumentPage.ocr_status)
                    .join(Document, Document.id == DocumentPage.document_id)
                    .where(
                        DocumentPage.ocr_status.in_(["pending", "running"]),
                        Document.deleted_at.is_(None),
                    )
                )
            ).all()
            ocr_docs = {r[0] for r in rows}
            for _, page_id, status in rows:
                if status == "running":
                    page = await session.get(DocumentPage, page_id)
                    if page is not None:
                        page.ocr_status = "pending"  # 중단된 페이지 재시도
            # 원래 실행의 언어·품질로 재개한다 — 기본값으로 다시 돌리면 한 문서 안에
            # DPI가 섞이고, 사용자가 고른 고품질/저메모리 선택이 조용히 무시된다.
            for doc_id in ocr_docs:
                last = (
                    await session.execute(
                        select(OcrRun.language, OcrRun.render_dpi)
                        .where(OcrRun.document_id == doc_id)
                        .order_by(OcrRun.created_at.desc())
                        .limit(1)
                    )
                ).first()
                if last is not None:
                    ocr_options[doc_id] = (
                        last[0],
                        dpi_to_quality.get(last[1], "standard"),
                    )
            await session.commit()
        for doc_id in ocr_docs:
            language, quality = ocr_options.get(doc_id, ("kor+eng", "standard"))
            self.enqueue_ocr(
                doc_id,
                f"recovery-{uuid.uuid4().hex[:8]}",
                language=language,
                quality=quality,
            )
        to_enqueue.extend(ocr_docs)

        # RUNNING/QUEUED로 남은 stale 잡 정리 — OCR은 위에서 재개한 문서를 제외하고,
        # 청크·요약은 재개 지점이 없어 실패로 확정해 사용자가 다시 요청하게 한다.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from app.models.document import DocumentJob
        from app.models.enums import JobStatus, JobType, SummaryRunStatus
        from app.models.summary import SummaryRun

        async with get_session_factory()() as session:
            stale_stmt = select(DocumentJob).where(
                DocumentJob.job_type.in_(
                    [JobType.OCR_DOCUMENT, JobType.CHUNK_REBUILD, JobType.SUMMARIZE]
                ),
                DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            if ocr_docs:
                # 방금 재개한 OCR 문서의 잡은 살아 있어야 한다
                stale_stmt = stale_stmt.where(
                    (DocumentJob.job_type != JobType.OCR_DOCUMENT)
                    | DocumentJob.document_id.notin_(ocr_docs)
                )
            stale_jobs = (await session.execute(stale_stmt)).scalars().all()
            for job in stale_jobs:
                job.status = JobStatus.FAILED
                job.failure_code = "INTERRUPTED"
                job.completed_at = _dt.now(_UTC)
            # 중단된 요약 run도 잡과 같은 원칙으로 실패 확정한다.
            stale_runs = (
                (
                    await session.execute(
                        select(SummaryRun).where(
                            SummaryRun.status.in_(
                                [SummaryRunStatus.QUEUED, SummaryRunStatus.RUNNING]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            for run in stale_runs:
                run.status = SummaryRunStatus.FAILED
                run.error_code = "INTERRUPTED"
                run.completed_at = _dt.now(_UTC)
            await session.commit()

        # 이전 프로세스의 활성 Q&A 스트림(pending/streaming/finalizing)을 interrupted로
        # 확정한다 — 사용자 질문은 보존, 초안은 최종으로 승격하지 않는다(재시도 가능).
        from app.services.qa.stream_service import recover_interrupted_streams

        recovered_streams = await recover_interrupted_streams()

        if docs or recovered_streams:
            logger.info(
                "recovered_interrupted_jobs",
                requeued=len(to_enqueue),
                total=len(docs),
                streams=recovered_streams,
            )
        return len(to_enqueue)

    async def drain(self) -> None:
        """남은 작업 완료 대기 — 작업이 새 작업을 연쇄 등록(검증→추출)해도 전부 기다린다."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def has_pending(self) -> bool:
        return bool(self._tasks)


_runner: LocalTaskRunner | None = None


def get_task_runner() -> LocalTaskRunner:
    global _runner
    if _runner is None:
        _runner = LocalTaskRunner()
    return _runner


def reset_task_runner() -> None:
    global _runner
    _runner = None
