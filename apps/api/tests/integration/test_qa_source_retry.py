"""형식 재요청으로 얻은 답변도 실제 API의 출처 검증과 저장 경계를 통과해야 한다."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider
from tests.integration.test_qa_stream_api import collect, enable_deterministic, setup_doc


@pytest.mark.parametrize("valid_retry", [True, False])
async def test_retried_sources_are_verified_before_emission_and_persistence(
    client, monkeypatch, valid_retry,
):
    await enable_deterministic()
    doc_id, tid = await setup_doc(client)
    calls = []
    supported = "심장은 온몸에 혈액을 보내는 근육 기관이다."

    class Provider(OpenAICompatibleStreamingQaProvider):
        def stream_answer(self, request, token):
            def source(*_):
                calls.append(1)
                claim = {"type": "claim", "text": "출처 없는 예제 설명"}
                if len(calls) > 1:
                    claim = {"type": "claim", "text": supported, "sourceChunkIds": [
                        request.chunks[0].chunk_id if valid_retry else "not-owned",
                    ]}
                content = "\n".join(json.dumps(e, ensure_ascii=False) for e in [
                    claim, {"type": "final", "answerStatus": "answered"},
                ])
                return iter([json.dumps({"message": {"content": content}, "done": True})])
            self._line_source = source
            yield from super().stream_answer(request, token)

    async def provider(_db):
        return Provider(endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b",
                        api_key="", is_local=True)

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    events = await collect(client, doc_id, tid)
    assert len(calls) == 2
    expected = [supported] if valid_retry else []
    assert [e["text"] for e in events if e["type"] == "claim"] == expected
    message = events[-1]["message"]
    assert message["status"] == ("completed" if valid_retry else "insufficient_evidence")
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        rows = (await db.execute(select(QaClaim).where(QaClaim.message_id == saved.id))).scalars()
        assert [r.claim_text for r in rows] == expected
        assert saved.draft_content is None
        assert "출처 없는 예제" not in saved.content
