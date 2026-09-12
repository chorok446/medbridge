"""따옴표 없는 인용 뒤의 미지원 설명은 방출·저장하지 않는다."""

import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.services.qa.streaming import DeterministicStreamingQaProvider
from tests.integration.test_qa_stream_api import collect, enable_deterministic, setup_doc


@pytest.mark.parametrize("grounded", [True, False])
async def test_implicit_quote_is_verified_before_emission_and_storage(
    client, monkeypatch, grounded,
):
    await enable_deterministic()
    doc_id, tid = await setup_doc(client)
    source = "심장은 온몸에 혈액을 보내는 근육 기관이다"
    assertion = (
        "심장은 혈액을 보내는 기관이다."
        if grounded else "'심장'은 혈액을 펌프하는 주요 기관을 가리킨다."
    )
    text = f"{source}라는 문장에서 {assertion}"

    class Provider(DeterministicStreamingQaProvider):
        def stream_answer(self, request, token):
            yield {"type": "claim", "text": text,
                   "sourceChunkIds": [request.chunks[0].chunk_id]}
            yield {"type": "final", "answerStatus": "answered"}

    async def provider(_db):
        return Provider()

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    events = await collect(client, doc_id, tid)
    expected = [text] if grounded else []
    assert [e["text"] for e in events if e["type"] == "claim"] == expected
    message = events[-1]["message"]
    assert message["status"] == ("completed" if grounded else "insufficient_evidence")
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        rows = (await db.execute(select(QaClaim).where(QaClaim.message_id == saved.id))).scalars()
        assert [r.claim_text for r in rows] == expected
        assert saved.draft_content is None
        assert "펌프" not in saved.content
