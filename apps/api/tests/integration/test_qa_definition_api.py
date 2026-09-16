"""합성 PDF의 제목 비답변이 응답·draft·최종 저장에 남지 않는지 검증한다."""

import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.qa import QaClaim, QaMessage
from tests.integration.test_qa_api import (
    assistant_of,
    enable_deterministic,
    new_thread,
    upload_chunked,
)
from tests.integration.test_qa_stream_api import collect

DEFINITION = "예제 장치는 저장된 신호를 전송하는 기기이다."
BAD = "예제 장치"


@pytest.mark.parametrize("path", ["stream", "batch"])
@pytest.mark.parametrize("with_supported", [False, True])
async def test_definition_heading_never_reaches_response_or_storage(
    client, monkeypatch, path, with_supported
):
    await enable_deterministic()
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 60), "06. 예제 장치", fontname="korea", fontsize=18)
        page.insert_text((72, 150), DEFINITION, fontname="korea", fontsize=11)
        doc_id = await upload_chunked(client, pdf.tobytes())
    tid = await new_thread(client, doc_id)

    def claims(request):
        # 실제 업로드·추출·검색의 청크와 출처를 쓴다. ID에 다른 본문을 붙이지 않는다.
        heading = next(c for c in request.chunks if "06. 예제 장치" in c.text)
        yield {"text": BAD, "sourceChunkIds": [heading.chunk_id]}
        yield {"text": "출처: 예제 장치 (원문: 예제 장치)",
               "sourceChunkIds": [heading.chunk_id]}
        if with_supported:
            body = next(c for c in request.chunks if DEFINITION in c.text)
            yield {"text": DEFINITION, "sourceChunkIds": [body.chunk_id]}

    class Provider:
        provider_name = "deterministic"
        model_name = "synthetic"
        available = True
        is_local = True

        def stream_answer(self, request, token):
            for claim in claims(request):
                yield {"type": "claim", **claim}
            yield {"type": "final", "answerStatus": "answered"}

    async def provider(_db):
        return Provider()

    def answer(_self, request):
        return {"answerStatus": "answered", "answer": BAD + "[c0]",
                "claims": list(claims(request))}

    monkeypatch.setattr("app.services.qa.stream_service.get_qa_streaming_provider", provider)
    monkeypatch.setattr("app.services.qa.provider.DeterministicQaProvider.answer", answer)
    question = "예제 장치의 정의를 설명하는 원문 한 문장과 출처를 보여주세요."
    if path == "stream":
        events = await collect(client, doc_id, tid, question)
        assert [e["text"] for e in events if e["type"] == "claim"] == (
            [DEFINITION] if with_supported else []
        )
        message = events[-1]["message"]
    else:
        res = await client.post(f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
                                json={"question": question})
        assert res.status_code == 200
        message = assistant_of(res.json()["data"])
    assert message["status"] == ("completed" if with_supported else "insufficient_evidence")
    assert len(message["claims"]) == int(with_supported)
    assert message["content"] == (
        DEFINITION + "[c0]" if with_supported else
        "문서에서 충분한 근거를 찾지 못했어요. 다른 표현으로 다시 물어봐 주세요."
    )
    async with get_session_factory()() as db:
        saved = await db.get(QaMessage, uuid.UUID(message["id"]))
        rows = (await db.execute(select(QaClaim).where(
            QaClaim.message_id == saved.id
        ))).scalars().all()
        assert [row.claim_text for row in rows] == ([DEFINITION] if with_supported else [])
        assert saved.content == message["content"]
        assert saved.draft_content is None
