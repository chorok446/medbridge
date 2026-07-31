"""SqliteChunkVectorStore — 문서 범위·모델/차원 일치 필터링 통합 테스트."""

import uuid

from app.db.session import get_session_factory
from app.models.search import DocumentChunk
from app.services.search.chunking import rebuild_chunks
from app.services.search.vector import SqliteChunkVectorStore, pack_vector
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post(
        "/api/documents", files={"file": ("doc.pdf", data, "application/pdf")}
    )
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


class TestVectorStoreScoping:
    async def test_only_current_document_chunks_returned(self, client):
        doc_a = await upload_extracted(client, fx.single_column_korean(pages=1))
        doc_b = await upload_extracted(client, fx.single_column_korean(pages=1))

        async with get_session_factory()() as session:
            await rebuild_chunks(session, uuid.UUID(doc_a["id"]))
            await rebuild_chunks(session, uuid.UUID(doc_b["id"]))
            await session.commit()

        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        DocumentChunk.__table__.select().where(
                            DocumentChunk.document_id == uuid.UUID(doc_a["id"])
                        )
                    )
                ).all()
            )
            for row in rows:
                await session.execute(
                    DocumentChunk.__table__.update()
                    .where(DocumentChunk.id == row.id)
                    .values(
                        embedding_model="deterministic-test-v1",
                        embedding_dimension=8,
                        embedding_blob=pack_vector([0.1] * 8),
                    )
                )
            all_b = list(
                (
                    await session.execute(
                        DocumentChunk.__table__.select().where(
                            DocumentChunk.document_id == uuid.UUID(doc_b["id"])
                        )
                    )
                ).all()
            )
            for row in all_b:
                await session.execute(
                    DocumentChunk.__table__.update()
                    .where(DocumentChunk.id == row.id)
                    .values(
                        embedding_model="deterministic-test-v1",
                        embedding_dimension=8,
                        embedding_blob=pack_vector([0.9] * 8),
                    )
                )
            await session.commit()

            store = SqliteChunkVectorStore(session)
            candidates = await store.get_candidates(
                uuid.UUID(doc_a["id"]), model_name="deterministic-test-v1", dimension=8
            )
        assert candidates
        assert all(abs(c.vector[0] - 0.1) < 1e-6 for c in candidates)

    async def test_mismatched_model_or_dimension_excluded(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            await rebuild_chunks(session, uuid.UUID(doc["id"]))
            await session.commit()

        async with get_session_factory()() as session:
            rows = list(
                (
                    await session.execute(
                        DocumentChunk.__table__.select().where(
                            DocumentChunk.document_id == uuid.UUID(doc["id"])
                        )
                    )
                ).all()
            )
            for row in rows:
                await session.execute(
                    DocumentChunk.__table__.update()
                    .where(DocumentChunk.id == row.id)
                    .values(
                        embedding_model="old-model",
                        embedding_dimension=16,
                        embedding_blob=pack_vector([0.5] * 16),
                    )
                )
            await session.commit()

            store = SqliteChunkVectorStore(session)
            wrong_model = await store.get_candidates(
                uuid.UUID(doc["id"]), model_name="new-model", dimension=16
            )
            wrong_dim = await store.get_candidates(
                uuid.UUID(doc["id"]), model_name="old-model", dimension=8
            )
            correct = await store.get_candidates(
                uuid.UUID(doc["id"]), model_name="old-model", dimension=16
            )
        assert wrong_model == []
        assert wrong_dim == []
        assert len(correct) == len(rows)

    async def test_chunks_without_embedding_excluded(self, client):
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        async with get_session_factory()() as session:
            await rebuild_chunks(session, uuid.UUID(doc["id"]))
            await session.commit()
            store = SqliteChunkVectorStore(session)
            candidates = await store.get_candidates(
                uuid.UUID(doc["id"]), model_name="deterministic-test-v1", dimension=8
            )
        assert candidates == []
