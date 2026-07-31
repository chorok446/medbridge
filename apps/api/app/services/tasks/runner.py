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
        task = asyncio.get_running_loop().create_task(
            self._run_validate(document_id, correlation_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_validate(self, document_id: uuid.UUID, correlation_id: str) -> None:
        from app.services.tasks.validate import validate_document

        async with self._semaphore:
            try:
                await validate_document(document_id, correlation_id)
            except Exception:
                # 내부 예외는 로그로만 — 사용자에게는 document 상태로 전달된다
                logger.error("validate_task_crashed", document_id=str(document_id))

    async def recover_interrupted(self) -> int:
        """앱 재시작 시 중단된 작업 복구: queued/validating 문서를 다시 검증 큐에 넣는다."""
        async with get_session_factory()() as session:
            stmt = select(Document).where(
                Document.processing_status.in_(
                    [ProcessingStatus.QUEUED, ProcessingStatus.VALIDATING]
                ),
                Document.deleted_at.is_(None),
            )
            docs = list((await session.execute(stmt)).scalars())
            for doc in docs:
                if doc.processing_status == ProcessingStatus.VALIDATING:
                    # 중단된 검증을 재시도 가능 상태로 복귀 (내부 복구 경로)
                    doc.processing_status = ProcessingStatus.QUEUED
            await session.commit()
        for doc in docs:
            self.enqueue_validate(doc.id, f"recovery-{uuid.uuid4().hex[:8]}")
        if docs:
            logger.info("recovered_interrupted_jobs", count=len(docs))
        return len(docs)

    async def drain(self) -> None:
        """테스트·종료 시 남은 작업 완료 대기."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


_runner: LocalTaskRunner | None = None


def get_task_runner() -> LocalTaskRunner:
    global _runner
    if _runner is None:
        _runner = LocalTaskRunner()
    return _runner


def reset_task_runner() -> None:
    global _runner
    _runner = None
