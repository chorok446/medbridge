"""합성 PDF의 잘못된 출처 연결을 검증 뒤 응답·저장에도 일관되게 반영한다."""

import copy
import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.models.search import DocumentChunk
from tests.integration.test_qa_api import (
    assistant_of,
    enable_deterministic,
    new_thread,
    upload_chunked,
)
from tests.integration.test_qa_stream_api import collect
from tests.unit.test_qa_literal_rebinding import OTHER, TEXT


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("rewrite", [False, True])
async def test_literal_rebinding_reaches_response_and_storage(client, monkeypatch, path, rewrite):
    await enable_deterministic()
    with pymupdf.open() as pdf:
        for index, text in enumerate((TEXT, OTHER)):
            page = pdf.new_page()
            if index:
                # 제목 없는 짧은 두 본문은 페이지가 달라도 기존 규칙상 합쳐진다.
                # 출처 혼동을 재현할 수 있도록 두 번째 절의 실제 제목을 넣는다.
                page.insert_text((50, 60), "출력 분류", fontname="korea", fontsize=14)
            page.insert_text((50, 100), text, fontname="korea", fontsize=10)
        doc_id = await upload_chunked(client, pdf.tobytes())
    tid = await new_thread(client, doc_id)
    async with get_session_factory()() as db:
        rows = (await db.execute(select(DocumentChunk).where(
            DocumentChunk.document_id == uuid.UUID(doc_id),
        ))).scalars().all()
        before = {str(r.id): (r.normalized_text, r.content_hash, copy.deepcopy(r.source_refs_json))
                  for r in rows}
    assert len(before) == 2
    assert TEXT in {r[0] for r in before.values()}
    assert any(OTHER in r[0] for r in before.values())
    observed = []
    claim_text = TEXT.replace("기준은", "기준값은") if rewrite else TEXT

    def claim(request):
        target = next(c for c in request.chunks if c.text == TEXT)
        wrong = next(c for c in request.chunks if OTHER in c.text)
        observed.append(target.chunk_id)
        return {"text": claim_text, "sourceChunkIds": [wrong.chunk_id]}

    class Provider:
        provider_name = "deterministic"
        model_name = "synthetic"
        available = True
        is_local = True

        def stream_answer(self, request, token):
            yield {"type": "claim", **claim(request)}
            yield {"type": "final", "answerStatus": "answered"}

    async def provider(_db):
        return Provider()

    def answer(_self, request):
        return {"answerStatus": "answered", "answer": claim_text + "[c0]",
                "claims": [claim(request)]}

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    monkeypatch.setattr("app.services.qa.provider.DeterministicQaProvider.answer", answer)
    question = "예제 장치의 분류"
    if path == "stream":
        events = await collect(client, doc_id, tid, question)
        assert [e["text"] for e in events if e["type"] == "claim"] == (
            [] if rewrite else [TEXT]
        )
        message = events[-1]["message"]
    else:
        response = await client.post(f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
                                     json={"question": question})
        assert response.status_code == 200
        message = assistant_of(response.json()["data"])
    assert len(observed) == 1
    assert message["status"] == ("insufficient_evidence" if rewrite else "completed")
    assert len(message["claims"]) == (0 if rewrite else 1)
    if not rewrite:
        assert {r["pageNumber"] for r in message["claims"][0]["sourceRefs"]} == {1}
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        claims = (await db.execute(select(QaClaim).where(
            QaClaim.message_id == saved.id,
        ))).scalars().all()
        assert [c.claim_text for c in claims] == ([] if rewrite else [TEXT])
        if not rewrite:
            assert claims[0].source_chunk_ids_json == observed
            assert claims[0].source_refs_json == [
                {**r, "sectionTitle": None} for r in before[observed[0]][2]
            ]
        assert saved.content == message["content"]
        assert saved.draft_content is None
        rows = (await db.execute(select(DocumentChunk).where(
            DocumentChunk.document_id == uuid.UUID(doc_id),
        ))).scalars().all()
        assert {str(r.id): (r.normalized_text, r.content_hash, r.source_refs_json)
                for r in rows} == before
