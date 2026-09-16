"""합성 PDF의 상충 판정은 스트림 최종 상태와 저장된 출처까지 일치해야 한다."""

import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.services.qa.streaming import DeterministicStreamingQaProvider
from app.services.search.chunking import rebuild_chunks
from tests.integration.conftest import drain_jobs
from tests.integration.test_qa_stream_api import collect, enable_deterministic
from tests.unit.test_qa_conflict_scope import AXES, NEGATIVE, POSITIVE


@pytest.mark.parametrize(("texts", "hint", "expected"), [
    (AXES, "answered", "completed"),
    (AXES, "conflicting_evidence", "conflicting_evidence"),
    ([f"{POSITIVE} (원문: 기기에는 경고가 없다)", NEGATIVE],
     "answered", "conflicting_evidence"),
])
async def test_conflict_scope_reaches_final_response_and_storage(
    client, monkeypatch, texts, hint, expected,
):
    await enable_deterministic()
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        remaining = page.insert_textbox(
            pymupdf.Rect(60, 100, 535, 700), "\n".join(texts) * 3,
            fontname="korea", fontsize=10,
        )
        assert remaining > 0
        data = pdf.tobytes()
    response = await client.post(
        "/api/documents", files={"file": ("synthetic.pdf", data, "application/pdf")},
    )
    doc_id = response.json()["data"]["id"]
    await drain_jobs()
    async with get_session_factory()() as db:
        await rebuild_chunks(db, uuid.UUID(doc_id))
        await db.commit()
    response = await client.post(f"/api/documents/{doc_id}/qa/threads")
    tid = response.json()["data"]["thread"]["id"]

    class Provider(DeterministicStreamingQaProvider):
        def stream_answer(self, request, token):
            assert request.chunks
            for text in texts:
                yield {"type": "claim", "text": text,
                       "sourceChunkIds": [c.chunk_id for c in request.chunks]}
            yield {"type": "final", "answerStatus": hint}

    async def provider(_db):
        return Provider()

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    events = await collect(client, doc_id, tid, question="장치 필터 설명")
    assert [e["text"] for e in events if e["type"] == "claim"] == texts
    message = events[-1]["message"]
    assert message["status"] == expected
    assert [c["text"] for c in message["claims"]] == texts
    assert all(c["sourceRefs"] for c in message["claims"])
    assert "[c0]" in message["content"] and "[c1]" in message["content"]
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        rows = (await db.execute(select(QaClaim).where(
            QaClaim.message_id == saved.id,
        ).order_by(QaClaim.claim_index))).scalars().all()
        assert [r.claim_text for r in rows] == texts
        assert saved.status.value == expected
        assert saved.content == message["content"]
        assert saved.draft_content is None
