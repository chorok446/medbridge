"""상충의 한쪽 근거가 거부되면 최종 응답·DB에서 임시 주장과 인용을 제거한다."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider
from tests.integration.test_qa_stream_api import collect, enable_deterministic, setup_doc


@pytest.mark.parametrize("counterpart", ["unknown_source", "unsupported_number", "duplicate"])
async def test_lone_conflict_claim_is_not_saved_as_completed(client, monkeypatch, counterpart):
    await enable_deterministic()
    doc_id, tid = await setup_doc(client)
    supported = "심장은 온몸에 혈액을 보내는 근육 기관이다."

    class Provider(OpenAICompatibleStreamingQaProvider):
        def stream_answer(self, request, token):
            ids = [request.chunks[0].chunk_id]
            good = {"type": "claim", "text": supported, "sourceChunkIds": ids}
            bad = {**good, "sourceChunkIds": ["not-owned"]}
            if counterpart == "unsupported_number":
                bad = {**good, "text": "이 자료의 수치는 999999이다."}
            elif counterpart == "duplicate":
                bad = good.copy()  # 같은 주장을 두 번 보내도 상충 근거 두 개가 아니다.
            content = "\n".join(json.dumps(event, ensure_ascii=False) for event in [
                good, bad, {"type": "final", "answerStatus": "conflicting_evidence",
                            "followUpSuggestions": ["관련된 다른 질문"]},
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
    # 검증된 임시 주장은 방출되지만 final 상충 힌트 이후에는 최종 답으로 저장하지 않는다.
    assert [e["text"] for e in events if e["type"] == "claim"] == [supported]
    assert events[-1]["type"] == "completed"
    message = events[-1]["message"]
    assert message["status"] == "insufficient_evidence"
    assert message["claims"] == []
    assert supported not in message["content"]
    assert "[c0]" not in message["content"]
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        assert saved.status.value == "insufficient_evidence"
        assert saved.content == message["content"]
        assert saved.draft_content is None
        assert not saved.followups_json
        assert await db.scalar(select(func.count()).select_from(QaClaim).where(
            QaClaim.message_id == saved.id
        )) == 0
