"""저장형 계층 요약 실행기 — 체크포인트·재사용·재개·취소·revision·진행률 검증."""

import threading
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import (
    JobStatus,
    JobType,
    SummaryArtifactType,
    SummaryRunStatus,
)
from app.models.summary import SummaryNode, SummaryRun
from app.services.search.chunking import rebuild_chunks
from app.services.summary import executor as executor_mod
from app.services.summary import grouping as grouping_mod
from app.services.summary import service as summary_service
from app.services.summary.endpoint import SummaryNetworkError
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
    """결정론적 공급자 + 호출 횟수 기록. 호출 수가 곧 모델 비용이다.

    실행기는 노드를 동시에 돌리고 공급자 호출은 `asyncio.to_thread`로 나가므로 이
    카운터는 여러 스레드에서 증가한다. `+= 1`은 CPython에서 원자적이지 않아 락 없이는
    호출을 세다 놓치고, "재사용으로 모델을 부르지 않았다"는 검증이 조용히 통과한다.
    """

    def __init__(self) -> None:
        self._counter_lock = threading.Lock()
        self.group_calls = 0
        self.document_calls = 0

    def summarize_group(self, request):
        with self._counter_lock:
            self.group_calls += 1
        return super().summarize_group(request)

    def summarize_document(self, request):
        with self._counter_lock:
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


async def _run_executor(
    document_id, run_id, job_id, revision, chunk_hash, provider, *, include_sections=True
):
    return await execute_hierarchical_summary(
        get_session_factory(),
        document_id=document_id,
        run_id=run_id,
        job_id=job_id,
        provider=provider,
        chunks=await _load_chunks(document_id),
        learner_level="nursing_student",
        language="ko",
        include_sections=include_sections,
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
        # 재개 의미만 본다 — "몇 번째에서 실패했나"가 결정적이어야 하므로 순차로 고정한다.
        # 동시 실행 중 실패는 TestNodeConcurrency에서 따로 검증한다.
        _set_concurrency(monkeypatch, 1)
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

    async def test_sections_scale_with_document_not_fan_in(self, client, monkeypatch):
        """섹션은 최상위 레벨이 아니라 그 아래 레벨에서 나온다.

        최상위만 쓰면 섹션 수가 REDUCE_FAN_IN에 갇혀, 1,060페이지 문서도 10페이지
        문서도 똑같이 최대 8개 섹션을 받는다(실기기: 16,226청크 문서가 섹션 4개·580자로
        끝났다). 축약 트리는 문서가 클수록 깊어지므로, 최상위만 보면 큰 문서일수록
        요약이 짧아지는 역전이 생긴다.
        """
        monkeypatch.setattr(executor_mod, "REDUCE_FAN_IN", 2)
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        by_level: dict[int, int] = {}
        for node in nodes:
            by_level[node.level] = by_level.get(node.level, 0) + 1
        levels = sorted(by_level)
        assert len(levels) >= 2, f"reduce 레벨이 생기지 않았다: {by_level}"

        top_level, section_level = levels[-1], levels[-2]
        sections = [
            d for d in drafts if d.artifact_type == SummaryArtifactType.SECTION_SUMMARY
        ]
        assert len(sections) == by_level[section_level], (
            f"섹션이 {section_level}레벨({by_level[section_level]}개)이 아니라 "
            f"다른 곳에서 나왔다: 섹션 {len(sections)}개"
        )
        assert len(sections) > by_level[top_level], (
            f"섹션이 최상위 레벨({by_level[top_level]}개)에 갇혔다"
        )

    async def test_sections_keep_real_chunk_sources(self, client, monkeypatch):
        """섹션 출처는 노드 id가 아니라 실제 청크 id여야 한다.

        출처가 어긋나면 인용을 눌렀을 때 엉뚱한 페이지로 간다.
        """
        monkeypatch.setattr(executor_mod, "REDUCE_FAN_IN", 2)
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            leaf_ids = {
                cid
                for node in (
                    await s.execute(
                        select(SummaryNode).where(
                            SummaryNode.summary_run_id == run_id,
                            SummaryNode.level == 0,
                        )
                    )
                ).scalars().all()
                for cid in node.source_chunk_ids_json
            }
        sections = [
            d for d in drafts if d.artifact_type == SummaryArtifactType.SECTION_SUMMARY
        ]
        assert sections
        for section in sections:
            assert section.source_chunk_ids, "출처 없는 섹션이 저장됐다"
            assert set(section.source_chunk_ids) <= leaf_ids
            assert section.source_refs, "인용 렌더용 ref가 없다"

    async def test_sections_dropped_when_user_turns_them_off(self, client, monkeypatch):
        """사용자가 끈 구역 요약은 결정론적 경로에서도 만들지 않는다."""
        monkeypatch.setattr(executor_mod, "REDUCE_FAN_IN", 2)
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = CountingProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(
            doc_id, run_id, job_id, rev, chash, provider, include_sections=False
        )
        assert not [
            d for d in drafts if d.artifact_type == SummaryArtifactType.SECTION_SUMMARY
        ]


class TestAdaptiveSplitOnContextOverflow:
    """Ollama는 기기마다 다른 컨텍스트로 모델을 올리고 서버는 그 값을 알 수 없다.

    상한을 상수로 추측하는 대신, 컨텍스트 초과로 실패하면 입력을 절반으로 나눠
    적응해야 한다. 실기기 대형 문서가 이 지점에서 통째로 실패했다.
    """

    async def test_oversized_group_is_split_and_succeeds(self, client, monkeypatch):
        """분할 → **병합** → succeeded. 이 PR의 핵심 신규 경로다.

        조각 요약을 다시 합치지 못하면 노드 하나당 요약 하나 계약이 깨져 열화 폴백으로
        떨어진다. 병합 호출을 실제로 세지 않으면(예: 모든 청크를 거부하는 모의 모델)
        폴백만 돌면서도 테스트가 통과해 이 경로의 회귀 보호가 0이 된다.
        """
        # 청크가 한 그룹에 모두 들어가게 두고, 모델 쪽 상한만 좁혀 분할을 유도한다.
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 1_000_000)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 1_000_000)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=12))
        assert len(await _load_chunks(doc_id)) >= 4, "분할을 볼 만큼 청크가 없다"

        class ContextLimited(CountingProvider):
            """청크 2개까지는 요약하고, 그보다 많으면 컨텍스트 초과로 거부한다."""

            def __init__(self, limit: int = 2) -> None:
                super().__init__()
                self.limit = limit
                self.rejections = 0
                self.merge_calls = 0

            def summarize_group(self, request):
                # 병합 요청의 청크 id는 f"{group_id}p{i}" — 조각을 다시 합치는 호출이다.
                if any(
                    c.chunk_id.startswith(f"{request.group_id}p") for c in request.chunks
                ):
                    self.merge_calls += 1
                if len(request.chunks) > self.limit:
                    self.rejections += 1
                    raise SummaryNetworkError("context_overflow", "map_context_overflow")
                return super().summarize_group(request)

        provider = ContextLimited()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert provider.rejections > 0, "분할 경로가 실행되지 않았다"
        assert provider.merge_calls > 0, "조각을 다시 합치지 않았다 — 폴백만 돌았다"
        assert drafts, "분할 후에도 artifact를 만들지 못했다"
        # 분할해도 노드 하나당 요약 하나 계약은 유지되고, 열화로 떨어지지 않는다
        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        assert nodes
        for node in nodes:
            assert node.summary_text.strip()
            assert node.source_chunk_ids_json
            assert node.status == "succeeded", "병합에 성공했는데 열화로 저장됐다"

    async def test_dropped_part_is_not_claimed_as_a_source(self, client, monkeypatch):
        """요약에 반영되지 않은 청크가 출처로 남으면 '근거 보기'가 거짓말을 한다.

        사용자가 근거 버튼을 누르면 요약문과 무관한 페이지로 이동하고, 그 문장이 그
        페이지에 근거한 것으로 오인한다.
        """
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 4000)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 4000)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=4))
        chunks = await _load_chunks(doc_id)
        banned = chunks[-1].chunk_id

        class RejectsOneChunk(CountingProvider):
            """특정 청크가 든 요청은 어떤 크기로도 요약하지 못한다."""

            def summarize_group(self, request):
                if any(c.chunk_id == banned for c in request.chunks):
                    raise SummaryNetworkError("context_overflow", "map_context_overflow")
                return super().summarize_group(request)

        provider = RejectsOneChunk()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        assert nodes
        for node in nodes:
            assert banned not in node.source_chunk_ids_json, (
                "요약하지 못한 청크가 출처로 남았다"
            )
            assert node.source_chunk_ids_json, "살아남은 조각의 출처까지 사라졌다"

    async def test_original_failure_category_survives_total_part_failure(self, client):
        """조각이 전부 실패했을 때 범주를 덮으면 엉뚱한 안내가 뜬다.

        출력 길이 계약 위반(bad_response)을 context_overflow로 재라벨링하면 사용자는
        "메모리를 확보하라"는 안내를 받고, 메모리를 비워도 같은 실패를 반복한다.
        """
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class AlwaysTooLong(CountingProvider):
            def summarize_group(self, request):
                self.group_calls += 1
                raise SummaryNetworkError("bad_response", "map_summary_too_long")

        provider = AlwaysTooLong()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        with pytest.raises(SummaryNetworkError) as caught:
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert caught.value.category == "bad_response"
        assert caught.value.reason == "map_summary_too_long"

    async def test_split_gives_up_instead_of_looping_forever(self, client, monkeypatch):
        """단일 청크마저 넘으면 더 쪼갤 수 없다 — 무한 분할 대신 실패해야 한다."""
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 4000)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 4000)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class AlwaysOverflows(CountingProvider):
            def summarize_group(self, request):
                self.group_calls += 1  # 실패해도 호출 횟수는 센다 — 안 세면 단언이 공허하다
                raise SummaryNetworkError("context_overflow", "map_context_overflow")

        provider = AlwaysOverflows()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        with pytest.raises(SummaryNetworkError) as caught:
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert caught.value.category == "context_overflow"
        # 깊이 제한이 있으므로 호출 횟수가 폭발하지 않는다
        assert provider.group_calls < 40, f"분할 호출 {provider.group_calls}회 — 제한이 없다"

    async def test_degraded_fallback_is_not_reused_on_retry(self, client, monkeypatch):
        """열화 폴백을 succeeded로 저장하면 재시도해도 영원히 같은 축약본이 나온다."""
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 4000)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 4000)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=4))

        class MergeAlwaysFails(CountingProvider):
            """조각 하나는 요약하지만 둘 이상 합치는 건 거부한다."""

            def summarize_group(self, request):
                if len(request.chunks) > 1:
                    self.group_calls += 1
                    raise SummaryNetworkError("context_overflow", "map_context_overflow")
                return super().summarize_group(request)

        provider = MergeAlwaysFails()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        degraded = [n for n in nodes if n.status == "degraded"]
        assert degraded, "폴백 결과가 degraded로 표시되지 않았다"

        # 두 번째 실행은 degraded 노드를 재사용하지 않고 다시 모델을 부른다
        second = MergeAlwaysFails()
        run2_id, job2_id, rev2, chash2 = await _make_run(doc_id, second)
        await _run_executor(doc_id, run2_id, job2_id, rev2, chash2, second)
        assert second.group_calls > 0, "degraded 노드를 재사용해 모델을 부르지 않았다"

    async def test_output_truncation_does_not_trigger_splitting(self, client):
        """finish_length는 출력 문제라 입력을 나눠도 해결되지 않는다 — 호출만 증폭된다."""
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class AlwaysTruncates(CountingProvider):
            def summarize_group(self, request):
                self.group_calls += 1
                raise SummaryNetworkError("bad_response", "finish_length")

        provider = AlwaysTruncates()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        with pytest.raises(SummaryNetworkError):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert provider.group_calls == 1, f"분할 재시도 발생 ({provider.group_calls}회)"

    async def test_truncation_that_survived_a_bigger_budget_does_split(self, client):
        """공급자가 예산을 늘려 다시 부른 뒤에도 잘리면, 남은 수단은 입력을 줄이는 것뿐이다.

        실기기에서 이 경로가 막혀 있어 908노드 중 225번째가 3회 연속 같은 지점에서
        실패했고, 이미 성공한 675개 노드가 있는데도 문서 전체가 매번 버려졌다.
        """
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class TruncatesUntilSmall(CountingProvider):
            """청크가 1개를 넘으면 예산을 늘려도 계속 잘린다(= map_finish_length)."""

            def summarize_group(self, request):
                if len(request.chunks) > 1:
                    self.group_calls += 1
                    raise SummaryNetworkError("bad_response", "map_finish_length")
                return super().summarize_group(request)

        provider = TruncatesUntilSmall()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert provider.group_calls > 0, "분할 경로가 실행되지 않았다"
        assert drafts, "분할했는데도 artifact를 만들지 못했다"

    async def test_unrelated_failure_is_not_retried_by_splitting(self, client):
        """입력 크기와 무관한 실패까지 나눠 재시도하면 호출만 낭비한다."""
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class AuthBroken(CountingProvider):
            def summarize_group(self, request):
                self.group_calls += 1  # 실패해도 호출 횟수는 센다
                raise SummaryNetworkError("auth_failed", "http_401")

        provider = AuthBroken()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        with pytest.raises(SummaryNetworkError):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)
        assert provider.group_calls == 1, "분할 재시도가 일어나면 안 된다"


class TestSingleOversizedChunk:
    """build_groups는 긴 청크를 쪼개지 않는다 — 청크 하나가 상한을 넘으면 그 그룹은
    청크 1개짜리다. 거기서 분할을 포기하면 그 청크 하나 때문에 문서 전체가 실패한다.
    """

    async def test_single_chunk_group_splits_its_text(self, client, monkeypatch):
        # 청크를 하나씩 그룹으로 만들어 "청크 1개짜리 그룹"을 강제한다
        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 1)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 1)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=3))

        class RejectsLongText(CountingProvider):
            """긴 텍스트는 거부한다 — 청크를 나눌 수 없으니 텍스트를 나눠야 통과한다."""

            def summarize_group(self, request):
                self.group_calls += 1
                if sum(len(c.text) for c in request.chunks) > 500:
                    raise SummaryNetworkError("context_overflow", "map_context_overflow")
                return super().summarize_group(request)

        provider = RejectsLongText()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert drafts, "청크 하나 때문에 문서 전체가 실패했다"
        async with get_session_factory()() as s:
            nodes = (
                await s.execute(
                    select(SummaryNode).where(SummaryNode.summary_run_id == run_id)
                )
            ).scalars().all()
        for node in nodes:
            # 텍스트를 나눠도 출처는 원래 청크 그대로여야 한다
            assert node.source_chunk_ids_json

    def test_text_split_keeps_the_chunk_id(self):
        from app.services.summary.executor import _split_chunk_text
        from app.services.summary.provider import ChunkInput

        chunk = ChunkInput("c1", "제목", "가나다 " * 200, 1, 1)
        halves = _split_chunk_text(chunk)

        assert halves is not None
        assert [c.chunk_id for c in halves] == ["c1", "c1"]
        assert all(c.text.strip() for c in halves)

    def test_short_text_is_not_split(self):
        from app.services.summary.executor import _split_chunk_text
        from app.services.summary.provider import ChunkInput

        assert _split_chunk_text(ChunkInput("c1", None, "짧다", 1, 1)) is None


class TestSplitPoint:
    """분할은 문자 수 기준이어야 한다 — 개수로 나누면 입력이 줄지 않는 절반이 생긴다."""

    @staticmethod
    def _chunks(sizes: list[int]):
        from app.services.summary.provider import ChunkInput

        return [
            ChunkInput(
                chunk_id=f"c{i}",
                section_title=None,
                text="가" * n,
                page_start=1,
                page_end=1,
            )
            for i, n in enumerate(sizes)
        ]

    def test_large_chunk_is_isolated_from_the_small_ones(self):
        from app.services.summary.executor import _split_point

        # 5,000자 1개 + 100자 10개. 개수 기준(len//2=5)이면 앞 절반이 5,400자로 그대로다.
        chunks = self._chunks([5000] + [100] * 10)
        mid = _split_point(chunks)
        assert mid == 1
        assert sum(len(c.text) for c in chunks[:mid]) == 5000
        assert sum(len(c.text) for c in chunks[mid:]) == 1000

    def test_even_sizes_split_in_half(self):
        from app.services.summary.executor import _split_point

        assert _split_point(self._chunks([100] * 4)) == 2

    def test_never_returns_an_empty_half(self):
        from app.services.summary.executor import _split_point

        for sizes in ([1, 1], [10_000, 1], [1, 10_000]):
            mid = _split_point(self._chunks(sizes))
            assert 1 <= mid <= len(sizes) - 1


class ConcurrencyProbe(CountingProvider):
    """호출을 잠시 붙잡아 두고 동시에 실행 중인 호출 수의 최대값을 기록한다."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        super().__init__()
        self._state_lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self._hold = hold_seconds

    def summarize_group(self, request):
        with self._state_lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(self._hold)
            return super().summarize_group(request)
        finally:
            with self._state_lock:
                self._in_flight -= 1


def _set_concurrency(monkeypatch, value: int) -> None:
    """같은 레벨에서 동시에 실행할 노드 수를 고정한다(실기기에서는 기기 설정에서 온다)."""
    monkeypatch.setattr(executor_mod, "summary_node_concurrency", lambda: value)


def _many_groups(monkeypatch) -> None:
    """픽스처 본문을 여러 그룹으로 쪼갠다 — 기본값이면 한 그룹이라 동시성이 드러나지 않는다."""
    monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
    monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)


class TestNodeConcurrency:
    """같은 레벨의 노드는 서로 독립이므로 동시에 실행한다.

    실기기 문서는 map 노드가 794개였다. 순차로는 노드당 수 초 x 794 = 수 시간이라
    사용자가 요약을 끝까지 볼 수 없었다.
    """

    async def test_level_nodes_run_concurrently(self, client, monkeypatch):
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 4)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = ConcurrencyProbe()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        drafts = await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert drafts
        assert provider.group_calls > 1, "그룹이 하나뿐이면 이 검증은 아무것도 말하지 않는다"
        assert provider.max_in_flight > 1

    async def test_concurrency_never_exceeds_the_configured_limit(
        self, client, monkeypatch
    ):
        """공급자를 무제한으로 두드리지 않는다 — 로컬 모델은 큐만 쌓이고 타임아웃을 태운다."""
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 2)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = ConcurrencyProbe()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        assert provider.max_in_flight <= 2

    async def test_node_order_follows_input_not_completion(self, client, monkeypatch):
        """완료 순서가 뒤섞여도 노드 position은 입력 순서를 유지한다.

        position이 완료 순서를 따라가면 reduce 입력 순서가 실행마다 달라져 input_hash가
        바뀌고, 재사용이 통째로 무효가 된다(같은 문서를 매번 처음부터 다시 요약한다).
        """
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 4)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))

        class ReverseOrderProvider(CountingProvider):
            """뒤쪽 그룹일수록 빨리 끝나게 만들어 완료 순서를 입력 순서와 어긋나게 한다."""

            def summarize_group(self, request):
                time.sleep(max(0.0, 0.15 - 0.02 * len(request.chunks)))
                return super().summarize_group(request)

        provider = ReverseOrderProvider()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        async with get_session_factory()() as s:
            level0 = (
                await s.execute(
                    select(SummaryNode)
                    .where(SummaryNode.summary_run_id == run_id, SummaryNode.level == 0)
                    .order_by(SummaryNode.position)
                )
            ).scalars().all()
        assert len(level0) > 1
        assert [n.position for n in level0] == list(range(len(level0)))

        # 같은 문서를 다시 요약하면 전부 재사용돼야 한다 — 순서가 흔들렸다면 여기서 깨진다.
        second = CountingProvider()
        run2_id, job2_id, rev2, chash2 = await _make_run(doc_id, second)
        await _run_executor(doc_id, run2_id, job2_id, rev2, chash2, second)
        assert second.group_calls == 0

    async def test_first_failure_stops_launching_new_nodes(self, client, monkeypatch):
        """한 노드가 실패하면 아직 시작하지 않은 노드는 부르지 않는다.

        실패한 실행의 결과는 어차피 버려진다. 계속 부르면 사용자가 이미 오류를 본 뒤에도
        로컬 모델이 수백 번 더 돌아 기기를 점유한다.
        """
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 2)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))

        class FailsImmediately(CountingProvider):
            def summarize_group(self, request):
                super().summarize_group(request)
                raise RuntimeError("모의 공급자 실패")

        provider = FailsImmediately()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        with pytest.raises(RuntimeError):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        # 동시에 떠 있던 호출까지는 끝날 수 있지만 그 이상은 시작하지 않는다.
        assert provider.group_calls <= 2

    async def test_inflight_success_is_checkpointed_despite_a_sibling_failure(
        self, client, monkeypatch
    ):
        """형제 노드가 실패해 취소돼도, 모델 호출을 끝낸 노드는 저장된다.

        저장 직전에 취소를 받아들이면 이미 지불한 모델 호출이 통째로 버려지고 재시도가
        재사용할 노드가 없다 — 체크포인트를 둔 이유가 사라진다.
        """
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 4)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))

        class OneFailsRestSucceed(CountingProvider):
            """position 순서와 무관하게 정확히 한 호출만 실패시킨다."""

            def __init__(self) -> None:
                super().__init__()
                self._fail_lock = threading.Lock()
                self._failed = False

            def summarize_group(self, request):
                with self._fail_lock:
                    should_fail = not self._failed
                    self._failed = True
                if should_fail:
                    time.sleep(0.05)  # 형제들이 모델 호출을 끝낼 시간을 준다
                    raise RuntimeError("모의 공급자 실패")
                return super().summarize_group(request)

        provider = OneFailsRestSucceed()
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)
        with pytest.raises(RuntimeError):
            await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        # group_calls는 성공한 호출만 센다(실패 경로는 super()를 부르지 않는다).
        assert provider.group_calls > 0, "형제 노드가 없으면 이 검증은 아무것도 말하지 않는다"
        # 성공한 형제는 하나도 빠짐없이 체크포인트로 남아 있어야 한다.
        assert await _node_count(run_id) == provider.group_calls

    async def test_progress_counts_every_completed_node(self, client, monkeypatch):
        """동시 증가에서 completed_nodes를 잃지 않는다 — 진행률이 실제보다 낮게 멈춘다."""
        _many_groups(monkeypatch)
        _set_concurrency(monkeypatch, 4)
        doc_id = await _upload_chunked(client, fx.single_column_korean(pages=6))
        provider = ConcurrencyProbe(hold_seconds=0.02)
        run_id, job_id, rev, chash = await _make_run(doc_id, provider)

        await _run_executor(doc_id, run_id, job_id, rev, chash, provider)

        saved = await _node_count(run_id)
        assert saved > 1
        async with get_session_factory()() as s:
            run = await s.get(SummaryRun, run_id)
            assert run.completed_nodes == saved


class TestConcurrentFailureSelection:
    """여러 노드가 함께 실패했을 때 호출자에게 올릴 예외를 고르는 규칙."""

    def test_control_flow_wins_over_a_model_failure(self):
        """취소·revision 변경은 실패가 아니라 제어 흐름이다.

        모델 실패를 올리면 사용자가 직접 취소한 작업이 '요약 실패'로 기록되고,
        문서가 바뀌어 버려야 할 결과가 공급자 오류로 둔갑해 오류 리포트를 오염시킨다.
        """
        group = ExceptionGroup(
            "nodes",
            [RuntimeError("모델 실패"), SummaryCancelled(), SummaryRevisionChanged()],
        )
        assert isinstance(executor_mod._representative_error(group), SummaryCancelled)

    def test_revision_change_is_preferred_over_a_model_failure(self):
        group = ExceptionGroup("nodes", [RuntimeError("모델 실패"), SummaryRevisionChanged()])
        assert isinstance(
            executor_mod._representative_error(group), SummaryRevisionChanged
        )

    def test_model_failure_is_preserved_for_classification(self):
        """분류 가능한 공급자 실패는 원형 그대로 올린다 — 실패 코드가 여기서 정해진다."""
        original = SummaryNetworkError("context_overflow", "map_context")
        group = ExceptionGroup("nodes", [original])
        assert executor_mod._representative_error(group) is original

    def test_injected_cancellations_are_not_mistaken_for_the_cause(self):
        """형제 태스크를 취소하며 생긴 CancelledError가 실제 원인을 가리면 안 된다."""
        original = RuntimeError("진짜 원인")
        group = ExceptionGroup("nodes", [original, ExceptionGroup("nested", [ValueError("x")])])
        assert executor_mod._representative_error(group) is original


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
