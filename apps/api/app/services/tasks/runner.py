"""로컬 작업 실행기 — Redis/Dramatiq를 대체하는 인프로세스 비동기 실행.

- TaskRunner 인터페이스 뒤에서 asyncio 태스크로 실행, 동시 실행 수 제한
- 작업 상태는 SQLite document_jobs에 저장 (앱 종료 시에도 상태 보존)
- 앱 재시작 시 queued/validating에 멈춘 문서를 복구한다
"""

import asyncio
import uuid
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
        self._semaphore = asyncio.Semaphore(
            max_concurrent or get_settings().max_concurrent_jobs
        )
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

    async def _run_chunk_rebuild(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.chunk_job import (
            mark_chunk_rebuild_crashed,
            run_chunk_rebuild_job,
        )

        async with self._semaphore:
            try:
                await run_chunk_rebuild_job(document_id, correlation_id)
            except Exception:
                logger.error("chunk_rebuild_task_crashed", document_id=str(document_id))
                try:
                    await mark_chunk_rebuild_crashed(document_id)
                except Exception:
                    logger.error(
                        "mark_chunk_rebuild_crashed_failed", document_id=str(document_id)
                    )

    async def _run_ocr(
        self, document_id: uuid.UUID, correlation_id: str, language: str, quality: str
    ) -> None:
        from app.services.tasks.ocr_job import mark_ocr_job_crashed, run_ocr_job

        async with self._semaphore:
            try:
                await run_ocr_job(
                    document_id, correlation_id, language=language, quality=quality
                )
            except Exception:
                logger.error("ocr_job_crashed", document_id=str(document_id))
                try:
                    await mark_ocr_job_crashed(document_id)
                except Exception:
                    logger.error("mark_ocr_crashed_failed", document_id=str(document_id))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_extract(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.extract import extract_document, mark_extraction_crashed

        async with self._semaphore:
            try:
                await extract_document(document_id, correlation_id)
            except Exception:
                logger.error("extract_task_crashed", document_id=str(document_id))
                try:
                    await mark_extraction_crashed(document_id)
                except Exception:
                    logger.error("mark_extract_crashed_failed", document_id=str(document_id))

    async def _run_validate(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.validate import mark_validation_crashed, validate_document

        async with self._semaphore:
            try:
                await validate_document(document_id, correlation_id)
            except Exception:
                logger.error("validate_task_crashed", document_id=str(document_id))
                # 크래시 시 문서가 validating에 고착되지 않도록 실패로 확정한다
                try:
                    await mark_validation_crashed(document_id)
                except Exception:
                    logger.error("mark_crashed_failed", document_id=str(document_id))

    async def recover_interrupted(self) -> int:
        """앱 재시작 시 중단 작업 복구.

        - queued/validating/uploaded: 다시 검증 큐로 (uploaded는 큐 등록 전에 중단된 경우)
        - created/uploading: 파일 저장이 보장되지 않으므로 실패로 확정 (재업로드 안내)
        """
        to_enqueue: list[uuid.UUID] = []
        to_extract: list[uuid.UUID] = []
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
                    doc.processing_status = ProcessingStatus.FAILED  # 내부 복구 경로
                    doc.storage_key = None
                    doc.failure_code = "VALIDATION_FAILED"
                    doc.failure_message = (
                        "업로드가 중단되었습니다. 파일을 다시 업로드해 주세요."
                    )
                elif doc.processing_status == ProcessingStatus.EXTRACTING:
                    # 중단된 추출 재실행 (페이지 단위 교체 저장이라 안전)
                    to_extract.append(doc.id)
                else:
                    if doc.processing_status != ProcessingStatus.QUEUED:
                        doc.processing_status = ProcessingStatus.QUEUED  # 내부 복구 경로
                    to_enqueue.append(doc.id)
            await session.commit()
        for doc_id in to_enqueue:
            self.enqueue_validate(doc_id, f"recovery-{uuid.uuid4().hex[:8]}")
        for doc_id in to_extract:
            self.enqueue_extract(doc_id, f"recovery-{uuid.uuid4().hex[:8]}")
        to_enqueue.extend(to_extract)

        # 중단된 OCR 복구: pending/running 페이지가 남은 문서를 다시 실행
        from app.models.extraction import DocumentPage

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
            await session.commit()
        for doc_id in ocr_docs:
            self.enqueue_ocr(doc_id, f"recovery-{uuid.uuid4().hex[:8]}")
        to_enqueue.extend(ocr_docs)

        # 대기 페이지 없이 RUNNING/QUEUED로 남은 stale OCR 잡 정리 (H5: 영구 차단 방지)
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from app.models.document import DocumentJob
        from app.models.enums import JobStatus, JobType

        async with get_session_factory()() as session:
            stale_stmt = select(DocumentJob).where(
                DocumentJob.job_type == JobType.OCR_DOCUMENT,
                DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            if ocr_docs:
                stale_stmt = stale_stmt.where(DocumentJob.document_id.notin_(ocr_docs))
            stale_jobs = (await session.execute(stale_stmt)).scalars().all()
            for job in stale_jobs:
                job.status = JobStatus.FAILED
                job.failure_code = "INTERRUPTED"
                job.completed_at = _dt.now(_UTC)
            await session.commit()

        # 청크 재생성은 페이지 단위 재개 지점이 없다 — 중단된 잡은 실패로 확정하고
        # 사용자가 다시 요청하게 한다(OCR stale job과 동일 원칙).
        async with get_session_factory()() as session:
            stale_chunk_jobs = (
                await session.execute(
                    select(DocumentJob).where(
                        DocumentJob.job_type == JobType.CHUNK_REBUILD,
                        DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                )
            ).scalars().all()
            for job in stale_chunk_jobs:
                job.status = JobStatus.FAILED
                job.failure_code = "INTERRUPTED"
                job.completed_at = _dt.now(_UTC)
            await session.commit()
        if docs:
            logger.info(
                "recovered_interrupted_jobs", requeued=len(to_enqueue), total=len(docs)
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
