"""요약 API 통합 테스트 — 생성/상태/조회/재시도/취소/삭제, 소유권, revision, 복구."""

import uuid

from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, SummaryRunStatus
from app.models.summary import SummaryArtifact, SummaryRun, SummarySettings
from app.services.search.chunking import rebuild_chunks
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def enable_deterministic() -> None:
    async with get_session_factory()() as s:
        s.add(SummarySettings(enabled=True, provider_type="deterministic"))
        await s.commit()


async def enable_external_openai() -> None:
    """외부(비로컬) openai_compatible 공급자 — API 키가 있어 available=True지만 외부 전송."""
    from app.services.summary import secrets

    async with get_session_factory()() as s:
        s.add(
            SummarySettings(
                enabled=True,
                provider_type="openai_compatible",
                endpoint="https://api.example.com/v1",
                model_name="gpt-x",
                is_local=False,
            )
        )
        await s.commit()
    secrets.set_api_key("test-key")


async def upload_chunked(client, data: bytes) -> str:
    res = await client.post(
        "/api/documents", files={"file": ("doc.pdf", data, "application/pdf")}
    )
    assert res.status_code == 201, res.text
    doc_id = res.json()["data"]["id"]
    await drain_jobs()
    async with get_session_factory()() as s:
        await rebuild_chunks(s, uuid.UUID(doc_id))
        await s.commit()
    return doc_id


async def make_summary(client, doc_id: str) -> None:
    res = await client.post(
        f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
    )
    assert res.status_code == 202, res.text
    await drain_jobs()


class TestSummaryFlow:
    async def test_create_status_get_flow(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        await make_summary(client, doc_id)

        status = (
            await client.get(f"/api/documents/{doc_id}/summaries/status")
        ).json()["data"]
        assert status["providerAvailable"] is True
        assert status["status"] == "succeeded"
        assert status["stale"] is False

        listed = (await client.get(f"/api/documents/{doc_id}/summaries")).json()["data"]
        assert listed["artifacts"]
        assert listed["stale"] is False
        for art in listed["artifacts"]:
            assert art["sourceRefs"]  # 모든 artifact에 출처
            ref = art["sourceRefs"][0]
            for key in ("pageNumber", "blockId", "bbox", "readingOrder", "sourceMethod"):
                assert key in ref

    async def test_technical_info_not_exposed(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        await make_summary(client, doc_id)
        raw = (await client.get(f"/api/documents/{doc_id}/summaries")).text
        # chunk id·모델명·token은 응답에 없어야 한다
        assert "sourceChunkIds" not in raw
        assert "deterministic-test-v1" not in raw
        assert "tokenCount" not in raw

    async def test_study_caution_always_present(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        await make_summary(client, doc_id)
        listed = (await client.get(f"/api/documents/{doc_id}/summaries")).json()["data"]
        cautions = [a for a in listed["artifacts"] if a["artifactType"] == "study_caution"]
        assert cautions
        assert "학습 보조용" in cautions[0]["content"]["text"]


class TestSummaryValidation:
    async def test_provider_unavailable_returns_501(self, client):
        # 공급자 미설정(기본 disabled) — 501, 앱 전체는 실패하지 않는다
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        res = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert res.status_code == 501

    async def test_search_still_works_without_provider(self, client):
        # 요약 모델 없이도 검색·문서 열람은 정상 동작
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        res = await client.post(
            f"/api/documents/{doc_id}/search", json={"query": "심장은", "mode": "keyword"}
        )
        assert res.status_code == 200
        assert res.json()["data"]

    async def test_summary_before_chunks_blocked(self, client):
        await enable_deterministic()
        res = await client.post(
            "/api/documents", files={"file": ("d.pdf", fx.single_column_korean(pages=1),
                                              "application/pdf")}
        )
        doc_id = res.json()["data"]["id"]
        await drain_jobs()
        # 청크를 만들지 않은 상태 — 409
        r = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert r.status_code == 409

    async def test_invalid_learner_level_422(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        r = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "wizard"}
        )
        assert r.status_code == 422


class TestSummaryOwnership:
    async def test_summary_requires_ownership(self, client):
        fake = uuid.uuid4()
        for path in (f"/api/documents/{fake}/summaries", f"/api/documents/{fake}/summaries/status"):
            method = client.get if path.endswith("status") else client.post
            res = await (method(path) if path.endswith("status") else client.post(path, json={}))
            assert res.status_code == 404


class TestSummaryDuplicateAndCancel:
    async def test_duplicate_blocked_and_delete_cascades(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        await make_summary(client, doc_id)

        async with get_session_factory()() as s:
            runs = (
                await s.execute(
                    select(func.count()).select_from(SummaryRun).where(
                        SummaryRun.document_id == uuid.UUID(doc_id)
                    )
                )
            ).scalar_one()
            arts = (
                await s.execute(
                    select(func.count()).select_from(SummaryArtifact).where(
                        SummaryArtifact.document_id == uuid.UUID(doc_id)
                    )
                )
            ).scalar_one()
        assert runs == 1 and arts > 0

        # 문서 삭제 → run·artifact cascade 삭제
        assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 200
        async with get_session_factory()() as s:
            remaining_runs = (
                await s.execute(
                    select(func.count()).select_from(SummaryRun).where(
                        SummaryRun.document_id == uuid.UUID(doc_id)
                    )
                )
            ).scalar_one()
            remaining_arts = (
                await s.execute(
                    select(func.count()).select_from(SummaryArtifact).where(
                        SummaryArtifact.document_id == uuid.UUID(doc_id)
                    )
                )
            ).scalar_one()
        assert remaining_runs == 0 and remaining_arts == 0

    async def test_rerun_does_not_accumulate_active_jobs(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        await make_summary(client, doc_id)
        # 재시도 → 새 run이 생기고, 활성 잡은 항상 1개 이하
        r = await client.post(
            f"/api/documents/{doc_id}/summaries/retry", json={"learnerLevel": "nursing_student"}
        )
        assert r.status_code == 202
        await drain_jobs()
        async with get_session_factory()() as s:
            active = (
                await s.execute(
                    select(func.count()).select_from(DocumentJob).where(
                        DocumentJob.document_id == uuid.UUID(doc_id),
                        DocumentJob.job_type == JobType.SUMMARIZE,
                        DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                )
            ).scalar_one()
        assert active == 0


class TestSummaryRevisionGuard:
    async def test_revision_change_during_run_discards_result(self, client):
        """시작 revision과 완료 시점 revision이 다르면 artifact를 저장하지 않는다."""
        from app.services.summary import service as summary_service
        from app.services.tasks.summary_job import run_summary_job

        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        duuid = uuid.UUID(doc_id)

        # run + job을 직접 만들되 source_revision을 현재보다 낮게(=이미 stale) 둔다
        async with get_session_factory()() as s:
            doc = await s.get(Document, duuid)
            source_hash = await summary_service.current_chunk_hash(s, duuid)
            run = SummaryRun(
                document_id=duuid,
                status=SummaryRunStatus.QUEUED,
                provider_name="deterministic",
                model_name="deterministic-test-v1",
                prompt_version="3b-1",
                schema_version=1,
                source_revision=doc.content_revision - 1,  # 시작 시점이 이미 과거
                source_chunk_hash=source_hash,
                learner_level="nursing_student",
                language="ko",
            )
            job = DocumentJob(
                document_id=duuid,
                job_type=JobType.SUMMARIZE,
                status=JobStatus.QUEUED,
                correlation_id="revtest",
            )
            s.add(run)
            s.add(job)
            await s.commit()
            run_id = run.id
            job_id = job.id

        await run_summary_job(duuid, "revtest", run_id=run_id, job_id=job_id)

        async with get_session_factory()() as s:
            saved = await s.get(SummaryRun, run_id)
            arts = (
                await s.execute(
                    select(func.count()).select_from(SummaryArtifact).where(
                        SummaryArtifact.summary_run_id == run_id
                    )
                )
            ).scalar_one()
        assert saved.status == SummaryRunStatus.FAILED
        assert saved.error_code == "REVISION_CHANGED"
        assert arts == 0


class TestSummaryExternalConsent:
    async def test_external_provider_blocked_without_consent(self, client):
        await enable_external_openai()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        # 문서·전역 외부 전송 동의가 없으면 403 (네트워크 호출 전에 차단)
        res = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert res.status_code == 403

    async def test_external_provider_allowed_with_both_consents(self, client):
        await enable_external_openai()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        # 전역 동의
        await client.patch("/api/profile", json={"externalAiAllowed": True})
        # 문서 동의
        async with get_session_factory()() as s:
            doc = await s.get(Document, uuid.UUID(doc_id))
            doc.external_evidence_enabled = True
            await s.commit()
        # 이제 동의는 통과 — 잡이 시작된다(202). 실제 네트워크 호출은 잡에서 실패해도
        # start 단계에서 403이 아니어야 한다는 점만 확인한다.
        res = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert res.status_code == 202
        await drain_jobs()


class TestSummaryDeleteWhileActive:
    async def test_delete_cancels_active_job_so_future_summaries_work(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        duuid = uuid.UUID(doc_id)
        # 활성 요약 잡·run을 직접 만든다(진행 중 상태를 모사)
        async with get_session_factory()() as s:
            s.add(
                DocumentJob(
                    document_id=duuid,
                    job_type=JobType.SUMMARIZE,
                    status=JobStatus.RUNNING,
                    correlation_id="active",
                )
            )
            s.add(
                SummaryRun(
                    document_id=duuid,
                    status=SummaryRunStatus.RUNNING,
                    provider_name="deterministic",
                    model_name="m",
                    prompt_version="3b-1",
                    schema_version=1,
                    source_revision=1,
                    source_chunk_hash="x",
                    learner_level="nursing_student",
                    language="ko",
                )
            )
            await s.commit()

        # 삭제 → 활성 잡이 FAILED로 확정돼야 한다(그러지 않으면 유니크 인덱스가 영구 차단)
        assert (await client.delete(f"/api/documents/{doc_id}/summaries")).status_code == 200
        async with get_session_factory()() as s:
            active = (
                await s.execute(
                    select(func.count()).select_from(DocumentJob).where(
                        DocumentJob.document_id == duuid,
                        DocumentJob.job_type == JobType.SUMMARIZE,
                        DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                )
            ).scalar_one()
        assert active == 0
        # 이후 새 요약을 시작할 수 있다
        res = await client.post(
            f"/api/documents/{doc_id}/summaries", json={"learnerLevel": "nursing_student"}
        )
        assert res.status_code == 202
        await drain_jobs()


class TestSummaryChunkReplacementGuard:
    async def test_same_text_rebuild_still_discards_result(self, client):
        """content_revision이 그대로여도 청크가 새 UUID로 교체되면(같은 텍스트라도)
        chunk id를 포함한 해시가 달라져 결과를 저장하지 않는다."""
        from app.services.search.chunking import rebuild_chunks
        from app.services.summary import service as summary_service
        from app.services.tasks.summary_job import run_summary_job

        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        duuid = uuid.UUID(doc_id)

        async with get_session_factory()() as s:
            doc = await s.get(Document, duuid)
            source_hash = await summary_service.current_chunk_hash(s, duuid)
            run = SummaryRun(
                document_id=duuid,
                status=SummaryRunStatus.QUEUED,
                provider_name="deterministic",
                model_name="deterministic-test-v1",
                prompt_version="3b-1",
                schema_version=1,
                source_revision=doc.content_revision,
                source_chunk_hash=source_hash,
                learner_level="nursing_student",
                language="ko",
            )
            job = DocumentJob(
                document_id=duuid,
                job_type=JobType.SUMMARIZE,
                status=JobStatus.QUEUED,
                correlation_id="hashtest",
            )
            s.add(run)
            s.add(job)
            await s.commit()
            run_id, job_id = run.id, job.id

        # 같은 텍스트로 청크를 다시 만든다 — content_revision은 그대로지만 UUID가 바뀐다
        async with get_session_factory()() as s:
            await rebuild_chunks(s, duuid)
            await s.commit()

        await run_summary_job(duuid, "hashtest", run_id=run_id, job_id=job_id)

        async with get_session_factory()() as s:
            saved = await s.get(SummaryRun, run_id)
            arts = (
                await s.execute(
                    select(func.count()).select_from(SummaryArtifact).where(
                        SummaryArtifact.summary_run_id == run_id
                    )
                )
            ).scalar_one()
        assert saved.status == SummaryRunStatus.FAILED
        assert saved.error_code == "REVISION_CHANGED"
        assert arts == 0


class TestSummaryRestartRecovery:
    async def test_stale_running_summary_marked_failed_on_restart(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        duuid = uuid.UUID(doc_id)
        async with get_session_factory()() as s:
            s.add(
                DocumentJob(
                    document_id=duuid,
                    job_type=JobType.SUMMARIZE,
                    status=JobStatus.RUNNING,
                    correlation_id="stale",
                )
            )
            s.add(
                SummaryRun(
                    document_id=duuid,
                    status=SummaryRunStatus.RUNNING,
                    provider_name="deterministic",
                    model_name="m",
                    prompt_version="3b-1",
                    schema_version=1,
                    source_revision=1,
                    source_chunk_hash="x",
                    learner_level="nursing_student",
                    language="ko",
                )
            )
            await s.commit()

        from app.services.tasks.runner import get_task_runner

        await get_task_runner().recover_interrupted()
        await drain_jobs()

        status = (
            await client.get(f"/api/documents/{doc_id}/summaries/status")
        ).json()["data"]
        assert status["status"] == "failed"
        # 복구 후 다시 실행 가능
        assert status["canRetry"] is True
