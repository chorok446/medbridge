"""요약 재기동 자동 재개와 실패 이력 보존 기한 통합 테스트."""

import hashlib
import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryArtifactType, SummaryRunStatus
from app.models.summary import SummaryArtifact, SummaryNode, SummaryRun, SummarySettings
from app.services.search.chunking import rebuild_chunks
from app.services.summary import service as summary_service
from app.services.summary.factory import get_summary_provider
from app.services.summary.grouping import build_groups
from app.services.summary.hierarchy import build_context_key, map_node_input_hash
from app.services.summary.pipeline import build_chunk_inputs
from app.services.summary.provider import (
    DeterministicSummaryProvider,
    GroupRequest,
    provider_checkpoint_fingerprint,
)
from app.services.summary.schema import bounded_evidence_chunk_ids
from app.services.summary.settings import PROMPT_VERSION, SCHEMA_VERSION
from app.services.tasks.runner import get_task_runner
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def _enable_deterministic() -> None:
    async with get_session_factory()() as session:
        session.add(SummarySettings(enabled=True, provider_type="deterministic"))
        await session.commit()


async def _enable_external() -> None:
    from app.services.summary import secrets

    async with get_session_factory()() as session:
        session.add(
            SummarySettings(
                enabled=True,
                provider_type="openai_compatible",
                endpoint="https://api.example.com/v1",
                model_name="gpt-x",
                is_local=False,
            )
        )
        await session.commit()
    secrets.set_api_key("restart-test-key")


async def _upload_chunked(client, pages: int = 1) -> uuid.UUID:
    response = await client.post(
        "/api/documents",
        files={
            "file": (
                "restart.pdf",
                fx.single_column_korean(pages=pages),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 201, response.text
    document_id = uuid.UUID(response.json()["data"]["id"])
    await drain_jobs()
    async with get_session_factory()() as session:
        await rebuild_chunks(session, document_id)
        await session.commit()
    return document_id


async def _make_interrupted_summary(
    document_id: uuid.UUID,
    *,
    attempt_count: int = 1,
    max_attempts: int = 3,
    status: SummaryRunStatus = SummaryRunStatus.RUNNING,
    resume_count: int = 0,
    identity_resumable: bool = True,
    fingerprint: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with get_session_factory()() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        provider = await get_summary_provider(session, resolve_identity=True)
        job_id = uuid.uuid4()
        job = DocumentJob(
            id=job_id,
            document_id=document_id,
            job_type=JobType.SUMMARIZE,
            status=(JobStatus.RUNNING if status == SummaryRunStatus.RUNNING else JobStatus.QUEUED),
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            correlation_id="restart-test",
        )
        run = SummaryRun(
            job_id=job_id,
            document_id=document_id,
            status=status,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            source_revision=document.content_revision,
            source_chunk_hash=await summary_service.current_chunk_hash(session, document_id),
            learner_level="nursing_student",
            language="ko",
            include_sections=False,
            include_prerequisites=True,
            resume_count=resume_count,
            provider_fingerprint=(
                provider_checkpoint_fingerprint(provider) if fingerprint is None else fingerprint
            ),
            provider_identity_resumable=identity_resumable,
        )
        session.add_all([job, run])
        await session.commit()
        return run.id, job.id


class TestSummaryAutomaticResume:
    async def test_restarts_exact_run_job_and_persisted_options(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client, pages=2)
        run_id, job_id = await _make_interrupted_summary(document_id)

        assert await get_task_runner().recover_interrupted() == 1
        await drain_jobs()

        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.status == SummaryRunStatus.SUCCEEDED
            assert job is not None and job.status == JobStatus.SUCCEEDED
            # 중단 전 1회 + 재개 1회가 아니라, 중단된 동일 시도를 이어서 완료한 1회다.
            assert job.attempt_count == 1
            assert run.resume_count == 1
            assert run.include_sections is False
            assert run.include_prerequisites is True
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(SummaryRun)
                    .where(SummaryRun.document_id == document_id)
                )
            ).scalar_one() == 1
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(SummaryArtifact)
                    .where(
                        SummaryArtifact.summary_run_id == run_id,
                        SummaryArtifact.artifact_type == SummaryArtifactType.SECTION_SUMMARY,
                    )
                )
            ).scalar_one() == 0

    async def test_restart_reuses_committed_node_in_same_run(self, client, monkeypatch):
        """실제 startup 복구가 같은 행을 이어 쓰고 완료 노드를 다시 호출하지 않는다."""

        from app.services.summary import executor as executor_mod
        from app.services.summary import grouping as grouping_mod
        from app.services.tasks import summary_job

        monkeypatch.setattr(grouping_mod, "GROUP_MAX_CHARS", 400)
        monkeypatch.setattr(grouping_mod, "GROUP_MIN_CHARS", 400)
        await _enable_deterministic()
        monkeypatch.setattr(executor_mod, "summary_node_concurrency", lambda: 1)
        document_id = await _upload_chunked(client, pages=6)
        run_id, job_id = await _make_interrupted_summary(document_id)

        async with get_session_factory()() as session:
            provider = await get_summary_provider(session, resolve_identity=True)
            chunks = await summary_service.load_chunk_snapshots(session, document_id)
            document = await session.get(Document, document_id)
            run = await session.get(SummaryRun, run_id)
            assert document is not None and run is not None
        groups = build_groups(build_chunk_inputs(chunks))
        assert len(groups) > 1
        context_key = build_context_key(
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            provider_fingerprint=provider_checkpoint_fingerprint(provider),
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            learner_level=run.learner_level,
            language=run.language,
            source_revision=run.source_revision,
            source_chunk_hash=run.source_chunk_hash,
        )
        first_group = groups[0]
        source_ids = [chunk.chunk_id for chunk in first_group.chunks]
        input_hash = map_node_input_hash(
            context_key, [(chunk.chunk_id, chunk.text) for chunk in first_group.chunks]
        )
        seeded_summary = provider.summarize_group(
            GroupRequest(
                group_id=first_group.group_id,
                section_title=first_group.section_title,
                chunks=first_group.chunks,
                learner_level=run.learner_level,
                language=run.language,
            )
        )
        checkpoint_id = uuid.uuid4()
        async with get_session_factory()() as session:
            session.add(
                SummaryNode(
                    id=checkpoint_id,
                    document_id=document_id,
                    summary_run_id=run_id,
                    level=0,
                    position=0,
                    status="succeeded",
                    input_hash=input_hash,
                    output_hash=hashlib.sha256(
                        seeded_summary.summary_text.encode("utf-8")
                    ).hexdigest(),
                    summary_text=seeded_summary.summary_text,
                    source_chunk_ids_json=bounded_evidence_chunk_ids(source_ids),
                    attempt_count=1,
                )
            )
            await session.commit()

        class CountingProvider(DeterministicSummaryProvider):
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.group_calls = 0
                self.document_calls = 0

            def summarize_group(self, request):
                with self.lock:
                    self.group_calls += 1
                return super().summarize_group(request)

            def summarize_document(self, request):
                with self.lock:
                    self.document_calls += 1
                return super().summarize_document(request)

        resumed_provider = CountingProvider()

        async def resolved_provider(_session, **_kwargs):
            return resumed_provider

        monkeypatch.setattr(summary_job, "get_summary_provider", resolved_provider)
        assert await get_task_runner().recover_interrupted() == 1
        await drain_jobs()

        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            node_ids = set(
                (
                    await session.execute(
                        select(SummaryNode.id).where(SummaryNode.summary_run_id == run_id)
                    )
                ).scalars()
            )
            assert run is not None and run.status == SummaryRunStatus.SUCCEEDED
            assert run.completed_nodes == run.planned_nodes == len(node_ids)
            assert job is not None and job.status == JobStatus.SUCCEEDED
            assert job.attempt_count == 1
        assert checkpoint_id in node_ids
        assert resumed_provider.group_calls == len(node_ids) - 1
        assert resumed_provider.document_calls == 1

    async def test_provider_mismatch_fails_before_new_attempt(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id, fingerprint="0" * 64)

        assert await get_task_runner().recover_interrupted() == 0
        await drain_jobs()
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "PROVIDER_CHANGED"
            assert run.failure_reason == "resume_provider_changed"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 0

    async def test_unverified_model_identity_never_auto_resumes(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id, identity_resumable=False)

        assert await get_task_runner().recover_interrupted() == 0
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "PROVIDER_CHANGED"
            assert run.failure_reason == "resume_provider_identity_unverified"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 0

    async def test_same_position_hash_mismatch_fails_before_provider_call(
        self, client, monkeypatch
    ):
        """복구한 run의 체크포인트 계획이 다르면 모델 전송 전에 안전 종료한다."""

        from app.services.summary import executor as executor_mod
        from app.services.tasks import summary_job

        await _enable_deterministic()
        monkeypatch.setattr(executor_mod, "summary_node_concurrency", lambda: 1)
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id)
        async with get_session_factory()() as session:
            session.add(
                SummaryNode(
                    document_id=document_id,
                    summary_run_id=run_id,
                    level=0,
                    position=0,
                    status="succeeded",
                    input_hash="0" * 64,
                    output_hash="1" * 64,
                    summary_text="현재 계획과 다른 체크포인트",
                    source_chunk_ids_json=[],
                )
            )
            await session.commit()

        class MustNotCallProvider(DeterministicSummaryProvider):
            group_calls = 0
            document_calls = 0

            def summarize_group(self, request):
                self.group_calls += 1
                return super().summarize_group(request)

            def summarize_document(self, request):
                self.document_calls += 1
                return super().summarize_document(request)

        provider = MustNotCallProvider()

        async def resolved_provider(_session, **_kwargs):
            return provider

        monkeypatch.setattr(summary_job, "get_summary_provider", resolved_provider)
        assert await get_task_runner().recover_interrupted() == 1
        await drain_jobs()

        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.status == SummaryRunStatus.FAILED
            assert run.error_code == "SUMMARY_RESUME_UNSAFE"
            assert run.failure_reason == "checkpoint_changed"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 1
        assert provider.group_calls == 0
        assert provider.document_calls == 0

    async def test_attempt_limit_is_bounded(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(
            document_id,
            attempt_count=3,
            max_attempts=3,
            status=SummaryRunStatus.QUEUED,
        )

        assert await get_task_runner().recover_interrupted() == 0
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "SUMMARY_ATTEMPTS_EXHAUSTED"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 3

    async def test_repeated_restart_limit_is_persisted_and_bounded(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id, attempt_count=1)

        # task를 실제로 spawn하지 않고 "복구 승인 → 프로세스가 다시 중단"을 세 번
        # 결정론적으로 재현한다. 매번 interrupted attempt는 환급되고 resume만 누적된다.
        for expected_resume_count in range(1, summary_service.MAX_SUMMARY_AUTO_RESUMES + 1):
            async with get_session_factory()() as session:
                resumes = await summary_service.prepare_interrupted_summary_resumes(session)
            assert [item.run_id for item in resumes] == [run_id]
            async with get_session_factory()() as session:
                run = await session.get(SummaryRun, run_id)
                job = await session.get(DocumentJob, job_id)
                assert run is not None and job is not None
                assert run.resume_count == expected_resume_count
                assert job.attempt_count == 0
                # run_summary_job의 진입 commit 직후 다시 종료된 상태를 재현한다.
                run.status = SummaryRunStatus.RUNNING
                job.status = JobStatus.RUNNING
                job.attempt_count += 1
                await session.commit()

        async with get_session_factory()() as session:
            assert await summary_service.prepare_interrupted_summary_resumes(session) == []
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "SUMMARY_RESUME_EXHAUSTED"
            assert run.resume_count == summary_service.MAX_SUMMARY_AUTO_RESUMES
            assert job is not None and job.status == JobStatus.FAILED
            # interrupted attempt는 복구 상한에 막힌 경우에도 provider 실패로 남지 않는다.
            assert job.attempt_count == 0

    async def test_source_hash_change_fails_before_new_attempt(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id)
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            assert run is not None
            run.source_chunk_hash = "f" * 64
            await session.commit()

        assert await get_task_runner().recover_interrupted() == 0
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "REVISION_CHANGED"
            assert run.failure_reason == "resume_source_changed"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 0

    async def test_external_consent_is_rechecked_before_resume(self, client):
        await _enable_external()
        document_id = await _upload_chunked(client)
        run_id, job_id = await _make_interrupted_summary(document_id)

        assert await get_task_runner().recover_interrupted() == 0
        async with get_session_factory()() as session:
            run = await session.get(SummaryRun, run_id)
            job = await session.get(DocumentJob, job_id)
            assert run is not None and run.error_code == "EXTERNAL_CONSENT_MISSING"
            assert job is not None and job.status == JobStatus.FAILED
            assert job.attempt_count == 0


class TestSummaryHistoryRetention:
    async def test_cleanup_is_bounded_and_never_removes_active_or_succeeded(self, client):
        await _enable_deterministic()
        document_id = await _upload_chunked(client)
        old = datetime.now(UTC) - timedelta(days=60)
        failed_id, succeeded_id, active_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        common = dict(
            document_id=document_id,
            provider_name="deterministic",
            model_name="deterministic-test-v1",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            source_revision=1,
            source_chunk_hash="a" * 64,
            learner_level="nursing_student",
            language="ko",
        )
        async with get_session_factory()() as session:
            session.add_all(
                [
                    SummaryRun(
                        id=failed_id,
                        status=SummaryRunStatus.FAILED,
                        completed_at=old,
                        created_at=old,
                        updated_at=old,
                        **common,
                    ),
                    SummaryRun(
                        id=succeeded_id,
                        status=SummaryRunStatus.SUCCEEDED,
                        completed_at=old,
                        created_at=old,
                        updated_at=old,
                        **common,
                    ),
                    SummaryRun(
                        id=active_id,
                        status=SummaryRunStatus.RUNNING,
                        created_at=old,
                        updated_at=old,
                        **common,
                    ),
                ]
            )
            # 관계 속성을 정의하지 않은 모델이므로 FK 순서를 명시한다.
            await session.flush()
            session.add_all(
                [
                    SummaryArtifact(
                        document_id=document_id,
                        summary_run_id=failed_id,
                        artifact_type=SummaryArtifactType.OVERVIEW,
                        position=0,
                        content_json={"text": "old"},
                        source_chunk_ids_json=[],
                        source_refs_json=[],
                    ),
                    SummaryNode(
                        document_id=document_id,
                        summary_run_id=failed_id,
                        level=0,
                        position=0,
                        status="succeeded",
                        input_hash="b" * 64,
                        output_hash="c" * 64,
                        summary_text="old",
                        source_chunk_ids_json=[],
                    ),
                ]
            )
            await session.commit()

        async with get_session_factory()() as session:
            assert (
                await summary_service.cleanup_summary_history(
                    session, retention_days=30, max_rows=1, batch_size=1
                )
                == 1
            )
        async with get_session_factory()() as session:
            assert await session.get(SummaryRun, failed_id) is not None
            assert await session.get(SummaryRun, succeeded_id) is not None
            assert await session.get(SummaryRun, active_id) is not None

        async with get_session_factory()() as session:
            assert (
                await summary_service.cleanup_summary_history(
                    session, retention_days=30, max_rows=10, batch_size=1
                )
                == 2
            )
        async with get_session_factory()() as session:
            assert await session.get(SummaryRun, failed_id) is None
            assert await session.get(SummaryRun, succeeded_id) is not None
            assert await session.get(SummaryRun, active_id) is not None
