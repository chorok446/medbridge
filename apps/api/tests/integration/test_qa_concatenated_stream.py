"""붙은 JSON을 파싱한 뒤에도 기존 출처·수치 검증과 저장 경계를 유지한다."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider
from tests.integration.test_qa_stream_api import collect, enable_deterministic, setup_doc


@pytest.mark.parametrize("bad_kind", ["unknown_source", "unsupported_number"])
async def test_adjacent_events_still_use_server_verification(client, monkeypatch, bad_kind):
    await enable_deterministic()
    doc_id, tid = await setup_doc(client)
    supported = "심장은 온몸에 혈액을 보내는 근육 기관이다."

    class Provider(OpenAICompatibleStreamingQaProvider):
        def stream_answer(self, request, token):
            ids = [request.chunks[0].chunk_id]
            bad = {"type": "claim", "text": supported, "sourceChunkIds": ["not-owned"]}
            if bad_kind == "unsupported_number":
                bad.update(text="이 자료의 수치는 999999이다.", sourceChunkIds=ids)
            content = "".join(json.dumps(event, ensure_ascii=False) for event in [
                bad, {"type": "claim", "text": supported, "sourceChunkIds": ids},
                {"type": "final", "answerStatus": "answered"},
            ])
            self._line_source = lambda *_: iter([
                json.dumps({"message": {"content": content}, "done": True})
            ])
            yield from super().stream_answer(request, token)

    async def provider(_db):
        return Provider(endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b",
                        api_key="", is_local=True)

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    events = await collect(client, doc_id, tid)
    assert [e["text"] for e in events if e["type"] == "claim"] == [supported]
    message = events[-1]["message"]
    assert message["status"] == "completed"
    assert message["content"] == supported + "[c0]"
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        rows = (await db.execute(select(QaClaim).where(
            QaClaim.message_id == saved.id
        ))).scalars().all()
        assert [r.claim_text for r in rows] == [supported]
        assert saved.draft_content is None
        assert rows[0].source_refs_json
        assert all(ref["pageNumber"] in (1, 2) for ref in rows[0].source_refs_json)
