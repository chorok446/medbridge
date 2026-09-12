"""문서의 최신 잡 조회 — 단일 정의.

취소·교체 감지(CAS 토큰)와 상태 표시가 전부 이 정렬 기준(created_at desc)에
의존한다. 호출부마다 따로 들고 있으면 한 곳의 tie-breaker·필터 변경이 '최신 잡'
판정을 경로별로 어긋나게 만들어, 취소된 실행이 남의 결과를 덮는 류의 버그가
특정 경로에서만 재발한다.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentJob
from app.models.enums import JobStatus, JobType


async def latest_job(
    session: AsyncSession, document_id: uuid.UUID, job_type: JobType
) -> DocumentJob | None:
    return (
        (
            await session.execute(
                select(DocumentJob)
                .where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == job_type,
                )
                .order_by(DocumentJob.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def claim_current_job_for_write(
    session: AsyncSession,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    job_type: JobType,
) -> bool:
    """활성 최신 잡의 writer lock을 선점하고 저장 자격을 확정한다.

    SQLite legacy transaction mode에서는 SELECT가 writer transaction을 시작하지 않는다.
    따라서 저장 경계의 첫 DML을 exact active-job 조건부 UPDATE로 만들어 취소와 직렬화한
    뒤, 같은 lock 아래에서 최신 잡인지 다시 확인한다. ``False``면 transaction을 닫아
    호출자가 늦은 결과를 저장할 여지를 남기지 않는다.
    """
    claimed_job_id = (
        await session.execute(
            update(DocumentJob)
            .where(
                DocumentJob.id == job_id,
                DocumentJob.document_id == document_id,
                DocumentJob.job_type == job_type,
                DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            # 값과 updated_at을 그대로 두는 writer-lock용 no-op UPDATE다. updated_at의
            # onupdate까지 막아 체크포인트마다 사용자에게 보이는 잡 시간이 바뀌지 않는다.
            .values(
                status=DocumentJob.status,
                updated_at=DocumentJob.updated_at,
            )
            .returning(DocumentJob.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if claimed_job_id is None:
        await session.rollback()
        return False

    latest = await latest_job(session, document_id, job_type)
    if latest is None or latest.id != job_id:
        await session.rollback()
        return False
    return True
