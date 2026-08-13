"""문서의 최신 잡 조회 — 단일 정의.

취소·교체 감지(CAS 토큰)와 상태 표시가 전부 이 정렬 기준(created_at desc)에
의존한다. 호출부마다 따로 들고 있으면 한 곳의 tie-breaker·필터 변경이 '최신 잡'
판정을 경로별로 어긋나게 만들어, 취소된 실행이 남의 결과를 덮는 류의 버그가
특정 경로에서만 재발한다.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentJob
from app.models.enums import JobType


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
