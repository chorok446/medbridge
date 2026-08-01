"""저장형 계층 요약 실행기 — 체크포인트·재사용·재개·취소·revision·진행률 검증."""

import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.summary import SummaryNode, SummaryRun
from app.services.search.chunking import rebuild_chunks
from app.services.summary import executor as executor_mod
from app.services.summary import grouping as grouping_mod
from app.services.summary import service as summary_service
from app.services.summary.executor import (
    SummaryCancelled,
    SummaryNoContent,
    SummaryRevisionChanged,
    execute_hierarchical_summary,
)
from app.services.summary.provider import DeterministicSummaryProvider, GroupSummary
from app.services.summary.settings import PROMPT_VERSION, SCHEMA_VERSION
from app.services.tasks.summary_job import _job_is_current
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


class CountingProvider(DeterministicSummaryProvider):
    """결정론적 공급자 + 호출 횟수 기록. 호출 수가 곧 모델 비용이다."""

    def __init__(self) -> None:
        self.group_calls = 0
        self.document_calls = 0

    def summarize_group(self, request):
        self.group_calls += 1
        return super().summarize_group(request)

    def summarize_document(self, request):
        self.document_calls += 1
        return super().summarize_document(request)


async def _upload_chunked(client, data: bytes) -> uuid.UUID:
    res = await client.post(
        "/api/documents", files={"file": ("doc.pdf", data, "application/pdf")}
    )
    assert res.status_code == 201, res.text
    doc_id = uuid.UUID(res.json()["data"]["id"])
    await drain_jobs()
    async with get_session_factory()() as s:
        await rebuild_chunks(s, doc_id)
        await s.commit()
    return doc_id


async def _make_run(document_id: uuid.UUID, provider) -> tuple[uuid.UUID, uuid.UUID, int, str]:
    """실행 대상 run·job을 만들고 (run_id, job_id, start_revision, start_hash)를 반환.

    활성 요약 잡은 문서당 하나만 허용되므로(부분 유니크 인덱스), 이전 잡이 남아 있으면
    먼저 종료 상태로 확정한다 — 실제 재시도 흐름과 같다.
    """
    async with get_session_factory()() as s:
        previous = (
            await s.execute(
                select(DocumentJob).where(
                    DocumentJob.document_id == document_id,
                    DocumentJob.job_type == JobType.SUMMARIZE,
                    DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            )
        ).scalars().all()
        for old in previous:
            old.status = JobStatus.FAILED
            old.failure_code = "SUPERSEDED"
        if previous:
            await s.commit()

    async with get_session_factory()() as s:
        doc = await s.get(Document, document_id)
        start_hash = await summary_service.current_chunk_hash(s, document_id)
        job = DocumentJob(
            document_id=document_id,
            job_type=JobType.SUMMARIZE,
            status=JobStatus.RUNNING,
            correlation_id="test",
        )
        run = SummaryRun(
            document_id=document_id,
            status=SummaryRunStatus.RUNNING,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            source_revision=doc.content_revision,
            source_chunk_hash=start_hash,
            learner_level="nursing_student",
            language="ko",
        )
        s.add(job)
        s.add(run)
        await s.commit()
        return run.id, job.id, doc.content_revision, start_hash


async def _run_executor(document_id, run_id, job_id, revision, chunk_hash, provider):
    return await execute_hierarchical_summary(
        get_session_factory(),
        document_id=document_id,
        run_id=run_id,
        job_id=job_id,
        provider=provider,
        chunks=await _load_chunks(document_id),
        learner_level="nursing_student",
        language="ko",
        include_sections=True,
        include_prerequisites=True,
        start_revision=revision,
        start_hash=chunk_hash,
        job_is_current=_job_is_current,
        current_chunk_hash=summary_service.current_chunk_hash,
    )


async def _load_chunks(document_id: uuid.UUID):
    async with get_session_factory()() as s:
        return await summary_service.load_chunk_snapshots(s, document_id)


async def _node_count(run_id: uuid.UUID) -> int:
    async with get_session_factory()() as s:
        return (
            await s.execute(
                select(func.count()).select_from(SummaryNode).where(
                    SummaryNode.summary_run_id == run_id
                )
            )
        ).scalar_one()


class TestCheckpointing:
    async def test_nodes_are_persisted_and_progress_tracked(self, client):
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert drafts
        saved = await _node_count(run_id)
        assert saved > 0
        assert provider.group_calls == saved  # 노드 하나당 모델 호출 하나
        assert provider.document_calls == 1  # 구조화 reduce는 마지막 한 번
        async with get_session_factory()() as s:
            run = await s.get(SummaryRun, run_id)
            assert run.planned_nodes == saved
            assert run.completed_nodes == saved

    async def test_second_run_reuses_nodes_without_calling_model(self, client):
        """앱을 껐다 켜거나 재시도해도 성공 노드를 재사용해 모델을 다시 부르지 않는다."""
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))
        first = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, first)
        await _run_executor(doc_id, run_id, job_id, rev, chash, first)
        assert first.group_calls > 0

        second = CountingProvider()
        run2_id, job2_id, rev2, chash2 = await _make_run(doc_id, second)
        drafts = await _run_executor(doc_id, run2_id, job2_id, rev2, chash2, second)

        assert drafts
        assert second.group_calls == 0  # map/reduce 노드는 전부 재사용
        assert second.document_calls == 1  # 구조화 reduce만 다시 수행
        async with get_session_factory()() as s:
            reused = (
                await s.execute(
                    select(func.count()).select_from(SummaryNode).where(
                        SummaryNode.summary_run_id == run2_id, SummaryNode.reused.is_(True)
                    )
                )
            ).scalar_one()
            assert reused == await _node_count(run2_id)

    async def test_partial_failure_resumes_from_checkpoint(self, client, monkeypatch):
        """중간에 실패해도 다음 실행은 성공한 노드만큼 모델 호출을 건너뛴다."""
        # 그룹을 잘게 나눠 여러 노드가 생기게 한다(픽스처 본문이 짧아 기본값이면 1그룹).
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))

        class FailsAfterFirst(CountingProvider):
            def summarize_group(self, request):
                if self.group_calls >= 1:
                    raise RuntimeError("모의 공급자 실패")
                return super().summarize_group(request)

        flaky = FailsAfterFirst()
        run_id, job_id, rev, chash = await _make_run(doc_id, flaky)
        with pytest.raises(RuntimeError):
            await _run_executor(doc_id, run_id, job_id, rev, chash, flaky)
        completed_before = await _node_count(run_id)
        assert completed_before == 1  # 성공한 첫 노드는 저장돼 있다

        healthy = CountingProvider()
        run2_id, job2_id, rev2, chash2 = await _make_run(doc_id, healthy)
        drafts = await _run_executor(doc_id, run2_id, job2_id, rev2, chash2, healthy)

        assert drafts
        total = await _node_count(run2_id)
        # 이미 성공한 2개는 재사용하고 나머지만 호출한다
        assert healthy.group_calls == total - completed_before


class TestGuards:
    async def test_cancelled_job_stops_without_artifacts(self, client):
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        # 잡을 취소 상태로 만든다 — 실행기는 첫 가드에서 멈춰야 한다
        async with get_session_factory()() as s:
            job = await s.get(DocumentJob, job_id)
            job.status = JobStatus.FAILED
            job.failure_code = "CANCELLED"
            await s.commit()

        with pytest.raises(SummaryCancelled):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert provider.group_calls == 0

    async def test_revision_change_stops_execution(self, client):
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        async with get_session_factory()() as s:
            doc = await s.get(Document, doc_id)
            doc.content_revision += 1
            await s.commit()

        with pytest.raises(SummaryRevisionChanged):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert provider.group_calls == 0

    async def test_no_content_raises(self, client):
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=1))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        with pytest.raises(SummaryNoContent):
            await execute_hierarchical_summary(
                get_session_factory(),
                document_id=doc_id,
                run_id=run_id,
                job_id=job_id,
                provider=provider,
                chunks=[],
                learner_level="nursing_student",
                language="ko",
                include_sections=True,
                include_prerequisites=True,
                start_revision=rev,
                start_hash=chash,
                job_is_current=_job_is_current,
                current_chunk_hash=summary_service.current_chunk_hash,
            )


class TestMultiLevelReduce:
    async def test_fan_in_creates_reduce_levels_and_keeps_sources(
        self, client, monkeypatch
    ):
        """fan-in을 넘으면 reduce 레벨이 생기고, 상위 노드 출처는 자식 출처의 합집합이다."""
        monkeypatch.setattr(executor_mod, "REDUCE_FAN_IN", 2)
        # 픽스처 본문은 짧아 기본 상한이면 한 그룹에 다 들어간다 — 그룹을 잘게 나눠
        # reduce 레벨이 실제로 생기게 한다.
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert drafts

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode)
                    .where(SummaryNode.summary_run_id == run_id)
                    .order_by(SummaryNode.level, SummaryNode.position)
                )
            ).scalars().all()
        levels = sorted({n.level for n in nodes})
        assert len(levels) >= 2, f"reduce 레벨이 생기지 않았다: {levels}"

        by_level = {lv: [n for n in nodes if n.level == lv] for lv in levels}
        level0_ids = {cid for n in by_level[0] for cid in n.source_chunk_ids_json}
        for lv in levels[1:]:
            for node in by_level[lv]:
                assert node.source_chunk_ids_json, "상위 노드에 출처가 없다"
                # 상위 노드 출처는 반드시 실제 청크 id여야 한다(노드 id가 새면 안 된다)
                assert set(node.source_chunk_ids_json) <= level0_ids

    async def test_reduce_node_sources_are_union_of_children(self, client, monkeypatch):
        monkeypatch.setattr(executor_mod, "REDUCE_FAN_IN", 2)
        # 픽스처 본문은 짧아 기본 상한이면 한 그룹에 다 들어간다 — 그룹을 잘게 나눠
        # reduce 레벨이 실제로 생기게 한다.
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode)
                    .where(SummaryNode.summary_run_id == run_id)
                    .order_by(SummaryNode.level, SummaryNode.position)
                )
            ).scalars().all()
        level0 = [n for n in nodes if n.level == 0]
        level1 = [n for n in nodes if n.level == 1]
        assert level1, "레벨 1 노드가 없다"
        # fan_in=2 → 레벨1 노드 i는 레벨0의 2i, 2i+1을 합친다
        for i, parent in enumerate(level1):
            children = level0[i * 2 : i * 2 + 2]
            expected: list[str] = []
            for child in children:
                for cid in child.source_chunk_ids_json:
                    if cid not in expected:
                        expected.append(cid)
            assert parent.source_chunk_ids_json == expected


class TestSourceOwnership:
    async def test_model_supplied_chunk_ids_are_ignored(self, client):
        """공급자가 가짜 chunk id를 돌려줘도 저장 출처는 서버 계산값이다."""
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class LyingProvider(CountingProvider):
            def summarize_group(self, request):
                real = super().summarize_group(request)
                return GroupSummary(
                    group_id=real.group_id,
                    section_title=real.section_title,
                    summary_text=real.summary_text,
                    source_chunk_ids=["존재하지-않는-청크"],
                )

        provider = LyingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        for node in nodes:
            assert "존재하지-않는-청크" not in node.source_chunk_ids_json
            assert node.source_chunk_ids_json
