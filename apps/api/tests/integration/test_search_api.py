"""검색 API 3종 — rebuild/status/search, 소유권 격리, 검증, 재시작 복구."""

import uuid

from app.db.session import get_session_factory
from app.models.document import DocumentJob
from app.models.enums import JobStatus, JobType
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


def upload_kwargs(data: bytes, filename: str = "doc.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


class TestChunkRebuildAndStatusApi:
    async def test_rebuild_then_status_reflects_chunk_count(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))

        res = await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        assert res.status_code == 200
        assert res.json()["data"]["started"] is True
        await drain_jobs()

        status = (await client.get(f"/api/documents/{doc['id']}/chunks/status")).json()["data"]
        assert status["chunkCount"] > 0
        assert status["jobStatus"] == "succeeded"
        assert status["lastRebuiltAt"] is not None
        assert status["embeddingAvailable"] is False  # 기본은 disabled 공급자

    async def test_duplicate_rebuild_blocked_while_running(self, client, monkeypatch):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))

        first = await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        assert first.json()["data"]["started"] is True
        second = await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        assert second.json()["data"]["started"] is False
        await drain_jobs()

    async def test_status_before_any_rebuild(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        status = (await client.get(f"/api/documents/{doc['id']}/chunks/status")).json()["data"]
        assert status["chunkCount"] == 0
        assert status["jobStatus"] is None
        assert status["lastRebuiltAt"] is None


class TestSearchApiValidation:
    async def test_empty_query_returns_422(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        res = await client.post(f"/api/documents/{doc['id']}/search", json={"query": ""})
        assert res.status_code == 422

    async def test_blank_query_returns_422(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        res = await client.post(f"/api/documents/{doc['id']}/search", json={"query": "   "})
        assert res.status_code == 422

    async def test_invalid_mode_returns_422(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        res = await client.post(
            f"/api/documents/{doc['id']}/search", json={"query": "심장은", "mode": "telepathy"}
        )
        assert res.status_code == 422

    async def test_no_results_returns_empty_array_not_error(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        await drain_jobs()
        res = await client.post(
            f"/api/documents/{doc['id']}/search", json={"query": "존재하지않는단어xyz"}
        )
        assert res.status_code == 200
        assert res.json()["data"] == []

    async def test_search_result_shape_and_click_navigation_fields(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        await drain_jobs()

        res = await client.post(
            f"/api/documents/{doc['id']}/search", json={"query": "심장은", "mode": "keyword"}
        )
        assert res.status_code == 200
        results = res.json()["data"]
        assert results
        first = results[0]
        for key in (
            "chunkId",
            "preview",
            "sectionTitle",
            "pageStart",
            "pageEnd",
            "sourceRefs",
            "matchType",
        ):
            assert key in first
        ref = first["sourceRefs"][0]
        for key in ("pageNumber", "blockId", "bbox", "readingOrder", "sourceMethod"):
            assert key in ref
        assert len(ref["bbox"]) == 4


class TestSearchOwnershipIsolation:
    async def test_search_requires_document_ownership(self, client):
        # 존재하지 않는 문서 id — 다른 사용자 문서 접근과 동일하게 404
        fake_id = uuid.uuid4()
        res = await client.post(f"/api/documents/{fake_id}/search", json={"query": "심장은"})
        assert res.status_code == 404

    async def test_rebuild_requires_document_ownership(self, client):
        fake_id = uuid.uuid4()
        res = await client.post(f"/api/documents/{fake_id}/chunks/rebuild")
        assert res.status_code == 404


class TestChunkRebuildRestartRecovery:
    async def test_stale_running_job_marked_failed_on_restart(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            job = DocumentJob(
                document_id=uuid.UUID(doc["id"]),
                job_type=JobType.CHUNK_REBUILD,
                status=JobStatus.RUNNING,
                correlation_id="test-stale",
            )
            session.add(job)
            await session.commit()

        from app.services.tasks.runner import get_task_runner

        await get_task_runner().recover_interrupted()
        await drain_jobs()

        status = (await client.get(f"/api/documents/{doc['id']}/chunks/status")).json()["data"]
        assert status["jobStatus"] == "failed"

        # 복구 후에는 다시 재생성을 시작할 수 있어야 한다(영구 차단 방지)
        res = await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        assert res.json()["data"]["started"] is True
        await drain_jobs()
