"""청크 생성 — reading_order 순서, 페이지 경계 source_ref, bbox 보존,
디지털·OCR 중복, 재실행 누적 방지, content_hash 중복 방지."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus
from app.models.extraction import DocumentPage, DocumentTable
from app.models.search import DocumentChunk, DocumentChunkGeneration
from app.services.extraction.ocr import OcrResult
from app.services.ocr import service as ocr_service
from app.services.search.chunking import (
    ChunkRebuildInProgress,
    ChunkRevisionChanged,
    _is_generation_lease_conflict,
    rebuild_chunks,
)
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs
from tests.integration.test_ocr_flow import fake_words


def upload_kwargs(data: bytes, filename: str = "doc.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


async def _rebuild(doc_id: str) -> int:
    async with get_session_factory()() as session:
        result = await rebuild_chunks(session, uuid.UUID(doc_id))
        await session.commit()
        return result.chunk_count


class TestChunkOrderAndSourceRefs:
    async def test_chunks_follow_reading_order_two_columns(self, client):
        doc = await upload_extracted(client, fx.two_column_english(pages=1))
        count = await _rebuild(doc["id"])
        assert count > 0

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars().all()
        joined = "\n".join(r.normalized_text for r in rows)
        # 왼쪽 열 전체가 오른쪽 열보다 먼저 나와야 한다 (읽기 순서 유지)
        assert joined.index("L0-0") < joined.index("R0-0")

    async def test_chunk_spanning_pages_keeps_all_source_refs(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        spanning = [r for r in rows if r.page_start != r.page_end]
        if spanning:
            chunk = spanning[0]
            pages_in_refs = {ref["pageNumber"] for ref in chunk.source_refs_json}
            assert chunk.page_start in pages_in_refs
            assert chunk.page_end in pages_in_refs
        # 모든 청크는 최소 1개의 source_ref를 가진다 (출처 없는 청크 금지)
        assert all(len(r.source_refs_json) >= 1 for r in rows)

    async def test_bbox_preserved_in_source_refs(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        for row in rows:
            for ref in row.source_refs_json:
                bbox = ref["bbox"]
                assert len(bbox) == 4
                x0, y0, x1, y1 = bbox
                assert x0 <= x1 and y0 <= y1
                assert ref["sourceMethod"] in ("digital", "ocr")
                assert isinstance(ref["readingOrder"], int)

    async def test_table_kept_as_own_chunk_not_flattened(self, client):
        doc = await upload_extracted(client, fx.with_table())
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        table_chunks = [r for r in rows if "심박수" in r.normalized_text]
        assert table_chunks
        # 표 청크는 제목 블록("표가 있는 문서")과 섞이지 않는다 (평탄화 금지)
        assert "표가 있는 문서" not in table_chunks[0].normalized_text

    async def test_table_chunk_uses_structured_markdown_not_flattened_block_text(self, client):
        doc = await upload_extracted(client, fx.with_table())
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            chunk_rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
            table_rows = (await session.execute(select(DocumentTable))).scalars().all()

        assert table_rows, "추출 파이프라인이 표를 감지하지 못했다 — 픽스처를 확인하라"
        table_chunks = [r for r in chunk_rows if "심박수" in r.normalized_text]
        assert table_chunks
        # 구조화된 markdown 표(파이프 구분)를 썼는지 확인한다 — block.text로 되돌아가면
        # 셀이 읽기 순서대로 한 줄에 나열돼 행·열 구분(파이프)이 사라진다.
        assert "|" in table_chunks[0].normalized_text


class TestChunkDigitalOcrDedup:
    async def test_digital_preferred_over_ocr_on_same_page(self, client, monkeypatch):
        fake = type(
            "FakeEngine",
            (),
            {
                "available": True,
                "version": "fake-1.0",
                "recognize_page": lambda self, pdf_path, page_number, **kw: OcrResult(
                    page_number=page_number, words=fake_words(["OCR", "전용", "문장", "예시"])
                ),
            },
        )()
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(
            client, fx.mixed_digital_and_scanned()
        )
        # 스캔 페이지도 OCR 처리
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalars().all()
        digital_page_chunks = [r for r in rows if r.page_start == 1]
        for chunk in digital_page_chunks:
            assert all(ref["sourceMethod"] == "digital" for ref in chunk.source_refs_json)


class TestChunkRebuildIdempotency:
    async def test_rerun_does_not_accumulate(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        first_count = await _rebuild(doc["id"])
        second_count = await _rebuild(doc["id"])
        assert first_count == second_count

        async with get_session_factory()() as session:
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert total == first_count

    async def test_no_duplicate_content_hash_within_rebuild(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            hashes = [
                r[0]
                for r in (
                    await session.execute(
                        select(DocumentChunk.content_hash).where(
                            DocumentChunk.document_id == uuid.UUID(doc["id"])
                        )
                    )
                ).all()
            ]
        assert len(hashes) == len(set(hashes))

    async def test_status_reports_pages_left_out_of_the_index(self, client):
        """일부만 억제된 문서도 그 사실을 화면에 알린다.

        failure_code는 청크가 **0개**일 때만 채워진다. 45쪽 중 40쪽이 저신뢰로
        빠져도 5쪽 분량 청크가 남으면 화면에는 아무 표시 없이 "검색 준비 완료"가
        된다 — 사용자는 문서의 90%가 검색·질문·요약에서 보이지 않는데 알 길이 없다.
        """
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        await _rebuild(doc["id"])

        # 한 쪽을 저신뢰로 표시한다(OCR이 읽었지만 못 믿는 상태).
        async with get_session_factory()() as session:
            page = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                    )
                )
            ).scalars().one()
            page.ocr_status = OcrRunStatus.OCR_LOW_CONFIDENCE.value
            await session.commit()

        status = (
            await client.get(f"/api/documents/{doc['id']}/chunks/status")
        ).json()["data"]

        assert status["suppressedPages"] == 1
        # 청크는 남아 있으므로 실패 코드로는 드러나지 않는다 — 그래서 이 필드가 필요하다.
        assert status["chunkCount"] > 0
        assert status["failureCode"] is None

    async def test_delete_document_cascades_chunks(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        await _rebuild(doc["id"])
        assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 200

        async with get_session_factory()() as session:
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert remaining == 0


class TestChunkGenerationSafety:
    def test_only_partial_unique_violation_is_classified_as_rebuild_conflict(self):
        lease = IntegrityError(
            "INSERT",
            {},
            Exception(
                "UNIQUE constraint failed: document_chunk_generations.document_id"
            ),
        )
        foreign_key = IntegrityError(
            "INSERT", {}, Exception("FOREIGN KEY constraint failed")
        )
        other_unique = IntegrityError(
            "INSERT",
            {},
            Exception(
                "UNIQUE constraint failed: document_chunk_generations.shadow_document_id"
            ),
        )

        assert _is_generation_lease_conflict(lease)
        assert not _is_generation_lease_conflict(foreign_key)
        assert not _is_generation_lease_conflict(other_unique)

    async def test_non_lease_integrity_error_is_not_mislabeled(
        self, client, monkeypatch
    ):
        from sqlalchemy.ext.asyncio import AsyncSession

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        real_commit = AsyncSession.commit
        commits = 0

        async def fail_generation_commit(session):
            nonlocal commits
            commits += 1
            if commits == 2:
                raise IntegrityError(
                    "INSERT", {}, Exception("FOREIGN KEY constraint failed")
                )
            await real_commit(session)

        monkeypatch.setattr(AsyncSession, "commit", fail_generation_commit)
        async with get_session_factory()() as session:
            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                await rebuild_chunks(session, uuid.UUID(doc["id"]))

    async def test_active_generation_query_uses_generation_index(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])
        async with get_session_factory()() as session:
            document = await session.get(Document, doc_id)
            assert document is not None and document.active_chunk_generation_id is not None
            plans = []
            for generation in (None, document.active_chunk_generation_id.hex):
                plans.extend(
                    row[3]
                    for row in (
                        await session.execute(
                            text(
                                "EXPLAIN QUERY PLAN SELECT id FROM document_chunks "
                                "WHERE document_id = :document_id "
                                "AND generation_id IS :generation_id "
                                "ORDER BY chunk_index"
                            ),
                            {
                                "document_id": doc_id.hex,
                                "generation_id": generation,
                            },
                        )
                    ).all()
                )
            active_filter_plan = [
                row[3]
                for row in (
                    await session.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT c.id FROM document_chunks c "
                            "WHERE c.document_id = :document_id "
                            "AND c.generation_id IS ("
                            "SELECT d.active_chunk_generation_id FROM documents d "
                            "WHERE d.id = c.document_id) ORDER BY c.chunk_index"
                        ),
                        {"document_id": doc_id.hex},
                    )
                ).all()
            ]
            hash_plan = [
                row[3]
                for row in (
                    await session.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT id FROM document_chunks "
                            "WHERE document_id = :document_id "
                            "AND generation_id IS :generation_id AND content_hash = :hash"
                        ),
                        {
                            "document_id": doc_id.hex,
                            "generation_id": document.active_chunk_generation_id.hex,
                            "hash": "0" * 64,
                        },
                    )
                ).all()
            ]
            indexes = {
                row[1]
                for row in (
                    await session.execute(text("PRAGMA index_list('document_chunks')"))
                ).all()
            }
        assert all("ix_document_chunks_doc_generation_order" in plan for plan in plans)
        assert any(
            "ix_document_chunks_doc_generation_order" in plan
            for plan in active_filter_plan
        )
        assert any(
            "ix_document_chunks_doc_generation_hash" in plan for plan in hash_plan
        )
        assert "ix_document_chunks_doc_order" not in indexes
        assert "ix_document_chunks_doc_hash" not in indexes

    async def test_revision_change_keeps_previous_active_generation(self, client, monkeypatch):
        """계획 뒤 원문이 바뀌면 완성된 shadow도 활성화하지 않는다."""
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=4))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])
        async with get_session_factory()() as session:
            before_doc = await session.get(Document, doc_id)
            assert before_doc is not None
            before_generation = before_doc.active_chunk_generation_id
            before_ids = list(
                (
                    await session.execute(
                        select(DocumentChunk.id)
                        .where(DocumentChunk.document_id == doc_id)
                        .order_by(DocumentChunk.chunk_index)
                    )
                ).scalars()
            )

        original_write = chunking._write_staging_batch
        revision_changed = False

        async def change_revision_after_first_batch(session, generation, chunks):
            nonlocal revision_changed
            written = await original_write(session, generation, chunks)
            if not revision_changed:
                revision_changed = True
                document = await session.get(Document, doc_id)
                assert document is not None
                document.content_revision += 1
                await session.commit()
            return written

        monkeypatch.setattr(chunking, "_write_staging_batch", change_revision_after_first_batch)
        async with get_session_factory()() as session:
            with pytest.raises(ChunkRevisionChanged):
                await rebuild_chunks(session, doc_id)

        async with get_session_factory()() as session:
            after_doc = await session.get(Document, doc_id)
            assert after_doc is not None
            assert after_doc.active_chunk_generation_id == before_generation
            assert (
                list(
                    (
                        await session.execute(
                            select(DocumentChunk.id)
                            .where(DocumentChunk.document_id == doc_id)
                            .order_by(DocumentChunk.chunk_index)
                        )
                    ).scalars()
                )
                == before_ids
            )
            generations = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunkGeneration)
                    .where(DocumentChunkGeneration.document_id == doc_id)
                )
            ).scalar_one()
            assert generations == 1  # 실패한 shadow는 삭제되고 이전 활성 generation만 남는다.

    async def test_cancellation_discards_shadow_and_keeps_previous_chunks(
        self, client, monkeypatch
    ):
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=4))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])
        async with get_session_factory()() as session:
            before_ids = set(
                (
                    await session.execute(
                        select(DocumentChunk.id).where(DocumentChunk.document_id == doc_id)
                    )
                ).scalars()
            )
            before_doc = await session.get(Document, doc_id)
            assert before_doc is not None
            before_generation = before_doc.active_chunk_generation_id

        original_write = chunking._write_staging_batch

        async def cancel_after_committed_batch(session, generation, chunks):
            await original_write(session, generation, chunks)
            raise asyncio.CancelledError

        monkeypatch.setattr(chunking, "_write_staging_batch", cancel_after_committed_batch)
        async with get_session_factory()() as session:
            with pytest.raises(asyncio.CancelledError):
                await rebuild_chunks(session, doc_id)

        async with get_session_factory()() as session:
            after_doc = await session.get(Document, doc_id)
            assert after_doc is not None
            assert after_doc.active_chunk_generation_id == before_generation
            assert (
                set(
                    (
                        await session.execute(
                            select(DocumentChunk.id).where(DocumentChunk.document_id == doc_id)
                        )
                    ).scalars()
                )
                == before_ids
            )
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunkGeneration)
                    .where(DocumentChunkGeneration.document_id == doc_id)
                )
            ).scalar_one() == 1

    async def test_cancellation_after_pointer_commit_never_deletes_active_generation(
        self, client, monkeypatch
    ):
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=3))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])
        async with get_session_factory()() as session:
            before = await session.get(Document, doc_id)
            assert before is not None
            before_generation = before.active_chunk_generation_id

        original_cleanup = chunking._cleanup_inactive_generations
        cleanup_calls = 0

        async def cancel_post_activation_cleanup(document_id, active_generation_id):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 2:
                raise asyncio.CancelledError
            return await original_cleanup(document_id, active_generation_id)

        monkeypatch.setattr(
            chunking,
            "_cleanup_inactive_generations",
            cancel_post_activation_cleanup,
        )
        async with get_session_factory()() as session:
            with pytest.raises(asyncio.CancelledError):
                await rebuild_chunks(session, doc_id)

        async with get_session_factory()() as session:
            after = await session.get(Document, doc_id)
            assert after is not None
            assert after.active_chunk_generation_id != before_generation
            active = await session.get(
                DocumentChunkGeneration, after.active_chunk_generation_id
            )
            assert active is not None and active.status == "active"
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                )
            ).scalar_one() > 0

    async def test_large_rebuild_bounds_page_identity_map_and_write_batches(
        self, client, monkeypatch
    ):
        """문서 크기와 무관하게 page ORM 객체와 한 번의 writer batch가 상한을 가진다."""
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=40))
        doc_id = uuid.UUID(doc["id"])
        monkeypatch.setattr(chunking, "_PAGE_BATCH_SIZE", 3)
        monkeypatch.setattr(chunking, "_STAGING_BATCH_SIZE", 4)

        original_load = chunking._load_page_content
        original_write = chunking._write_staging_batch
        identity_sizes: list[int] = []
        write_sizes: list[int] = []

        async def observed_load(session, pages):
            result = await original_load(session, pages)
            identity_sizes.append(len(session.identity_map))
            return result

        async def observed_write(session, generation, chunks):
            write_sizes.append(len(chunks))
            return await original_write(session, generation, chunks)

        monkeypatch.setattr(chunking, "_load_page_content", observed_load)
        monkeypatch.setattr(chunking, "_write_staging_batch", observed_write)

        async with get_session_factory()() as session:
            result = await rebuild_chunks(session, doc_id)

        assert result.chunk_count > 4
        assert len(identity_sizes) >= 10
        # 이 fixture는 페이지당 14개 block이므로 3 pages + generation 정도만 남는다.
        # 40쪽 전체(560 block)와 함께 증가하면 이 상한/편차 검증이 깨진다.
        assert max(identity_sizes) <= 50
        assert max(identity_sizes[:-1]) - min(identity_sizes[:-1]) <= 3
        assert len(write_sizes) >= 2
        assert max(write_sizes) <= 4

    async def test_other_sqlite_writer_runs_between_staging_batches(
        self, client, monkeypatch
    ):
        """shadow batch commit 뒤에는 rebuild가 계속 살아 있어도 writer lock이 없다."""
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=8))
        doc_id = uuid.UUID(doc["id"])
        monkeypatch.setattr(chunking, "_STAGING_BATCH_SIZE", 2)
        original_write = chunking._write_staging_batch
        batch_committed = asyncio.Event()
        continue_rebuild = asyncio.Event()
        paused = False

        async def pause_after_committed_batch(session, generation, chunks):
            nonlocal paused
            written = await original_write(session, generation, chunks)
            if not paused:
                paused = True
                batch_committed.set()
                await continue_rebuild.wait()
            return written

        monkeypatch.setattr(
            chunking, "_write_staging_batch", pause_after_committed_batch
        )

        async def run_rebuild():
            async with get_session_factory()() as session:
                return await rebuild_chunks(session, doc_id)

        rebuild_task = asyncio.create_task(run_rebuild())
        await batch_committed.wait()

        async def independent_write():
            async with get_session_factory()() as session:
                document = await session.get(Document, doc_id)
                assert document is not None
                document.title = "청크 생성 중 저장된 제목"
                await session.commit()

        # rebuild가 writer transaction을 문서 전체 동안 잡았다면 busy_timeout(5초)까지
        # 기다리므로 이 2초 상한이 먼저 실패한다.
        await asyncio.wait_for(independent_write(), timeout=2.0)
        continue_rebuild.set()
        assert (await rebuild_task).chunk_count > 0

    async def test_next_rebuild_cleans_crash_orphan_generation(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=2))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])
        orphan_id = uuid.uuid4()
        orphan_fts_key = f"shadow:{orphan_id}"
        orphaned_at = datetime.now(UTC) - timedelta(minutes=10)
        async with get_session_factory()() as session:
            generation = DocumentChunkGeneration(
                id=orphan_id,
                document_id=doc_id,
                source_revision=1,
                shadow_document_id=orphan_fts_key,
                status="building",
                created_at=orphaned_at,
                updated_at=orphaned_at,
            )
            session.add(generation)
            await session.flush()
            session.add(
                DocumentChunk(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    generation_id=orphan_id,
                    chunk_index=0,
                    normalized_text="고아 shadow",
                    token_count=2,
                    page_start=1,
                    page_end=1,
                    source_refs_json=[{"pageNumber": 1}],
                    content_hash="f" * 64,
                )
            )
            await session.execute(
                text(
                    "INSERT INTO document_chunks_fts"
                    "(chunk_id, document_id, normalized_text, section_title) "
                    "VALUES (:chunk_id, :document_id, '고아 shadow', '')"
                ),
                {"chunk_id": str(uuid.uuid4()), "document_id": orphan_fts_key},
            )
            await session.commit()

        await _rebuild(doc["id"])

        async with get_session_factory()() as session:
            assert await session.get(DocumentChunkGeneration, orphan_id) is None
            orphan_chunks = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.generation_id == orphan_id)
                    .execution_options(include_inactive_chunks=True)
                )
            ).scalar_one()
            orphan_fts = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM document_chunks_fts WHERE document_id = :document_id"
                    ),
                    {"document_id": orphan_fts_key},
                )
            ).scalar_one()
        assert orphan_chunks == 0
        assert orphan_fts == 0

    async def test_running_owner_job_protects_old_heartbeat_until_job_stops(
        self, client
    ):
        from app.services.search.chunking import _cleanup_inactive_generations

        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        doc_id = uuid.UUID(doc["id"])
        stale_time = datetime.now(UTC) - timedelta(hours=1)
        generation_id = uuid.uuid4()
        async with get_session_factory()() as session:
            job = DocumentJob(
                document_id=doc_id,
                job_type=JobType.CHUNK_REBUILD,
                status=JobStatus.RUNNING,
                correlation_id="lease-test",
            )
            session.add(job)
            await session.flush()
            session.add(
                DocumentChunkGeneration(
                    id=generation_id,
                    document_id=doc_id,
                    source_revision=1,
                    shadow_document_id=f"shadow:{generation_id}",
                    owner_job_id=job.id,
                    status="building",
                    created_at=stale_time,
                    updated_at=stale_time,
                )
            )
            await session.commit()
            job_id = job.id

        await _cleanup_inactive_generations(doc_id, None)
        async with get_session_factory()() as session:
            assert await session.get(DocumentChunkGeneration, generation_id) is not None
            job = await session.get(DocumentJob, job_id)
            assert job is not None
            job.status = JobStatus.FAILED
            await session.commit()

        await _cleanup_inactive_generations(doc_id, None)
        async with get_session_factory()() as session:
            assert await session.get(DocumentChunkGeneration, generation_id) is None

    async def test_concurrent_rebuild_cannot_delete_or_activate_live_shadow(
        self, client, monkeypatch
    ):
        """partial unique lease가 두 writer를 CAS 전부터 직렬화한다."""
        from app.services.search import chunking

        doc = await upload_extracted(client, fx.single_column_korean(pages=5))
        doc_id = uuid.UUID(doc["id"])
        await _rebuild(doc["id"])

        entered = asyncio.Event()
        release = asyncio.Event()
        first_load = True
        original_load = chunking._load_page_batch

        async def pause_first_builder(session, document_id, cursor):
            nonlocal first_load
            if first_load:
                first_load = False
                entered.set()
                await release.wait()
            return await original_load(session, document_id, cursor)

        monkeypatch.setattr(chunking, "_load_page_batch", pause_first_builder)

        async def first_rebuild():
            async with get_session_factory()() as session:
                return await rebuild_chunks(session, doc_id)

        first_task = asyncio.create_task(first_rebuild())
        await entered.wait()
        async with get_session_factory()() as session:
            with pytest.raises(ChunkRebuildInProgress):
                await rebuild_chunks(session, doc_id)
        release.set()
        first_result = await first_task
        assert first_result.chunk_count > 0

        async with get_session_factory()() as session:
            document = await session.get(Document, doc_id)
            assert document is not None
            generations = list(
                (
                    await session.execute(
                        select(DocumentChunkGeneration).where(
                            DocumentChunkGeneration.document_id == doc_id
                        )
                    )
                ).scalars()
            )
            inactive_rows = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(
                        DocumentChunk.document_id == doc_id,
                        DocumentChunk.generation_id != document.active_chunk_generation_id,
                    )
                    .execution_options(include_inactive_chunks=True)
                )
            ).scalar_one()
        assert len(generations) == 1
        assert generations[0].id == document.active_chunk_generation_id
        assert generations[0].status == "active"
        assert inactive_rows == 0
