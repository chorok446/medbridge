"""uq_active_job_per_type 부분 유니크 인덱스가 실제로 동시 활성 잡을 막는지 검증.

0005의 인덱스는 status를 소문자 리터럴로 필터했으나 실제 저장 형태는 대문자 멤버
이름이라 무력화돼 있었다(0006에서 수정). 이 테스트는 회귀 방지용이다.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.session import get_session_factory
from app.models.document import DocumentJob
from app.models.enums import JobStatus, JobType
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def _upload(client) -> uuid.UUID:
    res = await client.post(
        "/api/documents",
        files={"file": ("d.pdf", fx.single_column_korean(pages=1), "application/pdf")},
    )
    assert res.status_code == 201
    await drain_jobs()
    return uuid.UUID(res.json()["data"]["id"])


class TestActiveJobIndex:
    async def test_second_active_same_type_job_rejected(self, client):
        doc_id = await _upload(client)
        async with get_session_factory()() as s:
            s.add(
                DocumentJob(
                    document_id=doc_id,
                    job_type=JobType.OCR_DOCUMENT,
                    status=JobStatus.RUNNING,
                    correlation_id="a",
                )
            )
            await s.commit()
        # 같은 (document_id, job_type)로 또 하나 활성(RUNNING) → 인덱스가 막아야 한다
        with pytest.raises(IntegrityError):
            async with get_session_factory()() as s:
                s.add(
                    DocumentJob(
                        document_id=doc_id,
                        job_type=JobType.OCR_DOCUMENT,
                        status=JobStatus.RUNNING,
                        correlation_id="b",
                    )
                )
                await s.commit()

    async def test_terminal_status_does_not_block(self, client):
        doc_id = await _upload(client)
        async with get_session_factory()() as s:
            s.add(
                DocumentJob(
                    document_id=doc_id,
                    job_type=JobType.OCR_DOCUMENT,
                    status=JobStatus.SUCCEEDED,  # 종료 상태는 인덱스 대상 아님
                    correlation_id="a",
                )
            )
            await s.commit()
        # 종료 상태 잡이 있어도 새 활성 잡은 허용된다
        async with get_session_factory()() as s:
            s.add(
                DocumentJob(
                    document_id=doc_id,
                    job_type=JobType.OCR_DOCUMENT,
                    status=JobStatus.QUEUED,
                    correlation_id="b",
                )
            )
            await s.commit()

    async def test_different_types_not_blocked(self, client):
        doc_id = await _upload(client)
        async with get_session_factory()() as s:
            s.add_all(
                [
                    DocumentJob(
                        document_id=doc_id,
                        job_type=JobType.OCR_DOCUMENT,
                        status=JobStatus.RUNNING,
                        correlation_id="a",
                    ),
                    DocumentJob(
                        document_id=doc_id,
                        job_type=JobType.CHUNK_REBUILD,
                        status=JobStatus.RUNNING,
                        correlation_id="b",
                    ),
                ]
            )
            await s.commit()  # 서로 다른 유형은 동시 허용
