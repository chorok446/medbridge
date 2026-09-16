"""대형 요약의 내부 coverage와 사용자 노출 evidence 분리 검증."""

import uuid

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.summary import SummaryArtifact, SummaryNode, SummaryRun
from app.services.search.chunking import rebuild_chunks
from app.services.summary import service as summary_service
from app.services.summary.pipeline import ChunkSnapshot
from app.services.summary.provider import DeterministicSummaryProvider
from app.services.summary.settings import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SUMMARY_EVIDENCE_CHUNK_LIMIT,
    SUMMARY_EVIDENCE_REF_LIMIT,
)
from app.services.tasks import summary_job
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def _upload_chunked(client) -> uuid.UUID:
    response = await client.post(
        "/api/documents",
        files={"file": ("doc.pdf", fx.single_column_korean(pages=1), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    document_id = uuid.UUID(response.json()["data"]["id"])
    await drain_jobs()
    async with get_session_factory()() as session:
        await rebuild_chunks(session, document_id)
        await session.commit()
    return document_id


async def test_large_internal_coverage_has_bounded_db_and_api_evidence(
    client, monkeypatch
):
    """5천 청크 coverage는 reduce까지 온전히 가지만 DB/API 근거는 작은 상한이다."""
    document_id = await _upload_chunked(client)
    count = 5_000
    chunks = [
        ChunkSnapshot(
            chunk_id=f"large-{i:04d}",
            section_title=f"구역 {i // 500}",
            text=f"청크 {i}의 검증 가능한 학습 내용이다.",
            page_start=i + 1,
            page_end=i + 1,
            source_refs=[
                {
                    "pageNumber": i + 1,
                    "blockId": f"large-{i}-a",
                    "bbox": [0, 0, 1, 1],
                    "readingOrder": i,
                    "sourceMethod": "digital",
                },
                {
                    "pageNumber": i + count + 1,
                    "blockId": f"large-{i}-b",
                    "bbox": [1, 1, 2, 2],
                    "readingOrder": i,
                    "sourceMethod": "digital",
                },
            ],
        )
        for i in range(count)
    ]

    class CapturingProvider(DeterministicSummaryProvider):
        document_coverage: list[str] = []

        def summarize_document(self, request):
            seen: set[str] = set()
            self.document_coverage = []
            for group in request.group_summaries:
                for chunk_id in group.source_chunk_ids:
                    if chunk_id not in seen:
                        seen.add(chunk_id)
                        self.document_coverage.append(chunk_id)
            return super().summarize_document(request)

    provider = CapturingProvider()

    async def fake_provider(session, *, resolve_identity=False):
        return provider

    async def fake_snapshots(session, current_document_id):
        assert current_document_id == document_id
        return chunks

    monkeypatch.setattr(summary_job, "get_summary_provider", fake_provider)
    monkeypatch.setattr(summary_service, "load_chunk_snapshots", fake_snapshots)

    async with get_session_factory()() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        source_hash = await summary_service.current_chunk_hash(session, document_id)
        job_id = uuid.uuid4()
        job = DocumentJob(
            id=job_id,
            document_id=document_id,
            job_type=JobType.SUMMARIZE,
            status=JobStatus.QUEUED,
            correlation_id="bounded-evidence-test",
        )
        run = SummaryRun(
            document_id=document_id,
            status=SummaryRunStatus.QUEUED,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            source_revision=document.content_revision,
            source_chunk_hash=source_hash,
            learner_level="nursing_student",
            language="ko",
        )
        session.add_all([job, run])
        await session.commit()
        run_id = run.id

    await summary_job.run_summary_job(
        document_id,
        "bounded-evidence-test",
        run_id=run_id,
        job_id=job_id,
    )

    expected_ids = [chunk.chunk_id for chunk in chunks]
    assert provider.document_coverage == expected_ids
    valid_ids = set(expected_ids)
    valid_refs = {
        (ref["pageNumber"], ref["blockId"])
        for chunk in chunks
        for ref in chunk.source_refs
    }
    async with get_session_factory()() as session:
        nodes = (
            await session.execute(
                select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
            )
        ).scalars().all()
        artifacts = (
            await session.execute(
                select(SummaryArtifact).where(SummaryArtifact.summary_run_id == run_id)
            )
        ).scalars().all()

    assert nodes
    assert len(artifacts) > 1
    for node in nodes:
        assert 0 < len(node.source_chunk_ids_json) <= SUMMARY_EVIDENCE_CHUNK_LIMIT
        assert set(node.source_chunk_ids_json) <= valid_ids
    for artifact in artifacts:
        assert 0 < len(artifact.source_chunk_ids_json) <= SUMMARY_EVIDENCE_CHUNK_LIMIT
        assert 0 < len(artifact.source_refs_json) <= SUMMARY_EVIDENCE_REF_LIMIT
        assert set(artifact.source_chunk_ids_json) <= valid_ids
        assert all(
            (ref["pageNumber"], ref["blockId"]) in valid_refs
            for ref in artifact.source_refs_json
        )

    listed = (
        await client.get(f"/api/documents/{document_id}/summaries")
    ).json()["data"]
    assert len(listed["artifacts"]) == len(artifacts)
    assert all(
        0 < len(artifact["sourceRefs"]) <= SUMMARY_EVIDENCE_REF_LIMIT
        for artifact in listed["artifacts"]
    )
