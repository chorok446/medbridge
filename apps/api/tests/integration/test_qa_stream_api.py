"""Q&A 스트리밍 API 통합 테스트 — 프로토콜·출처·취소·복구·revision·동의·동시성."""

import json
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.document import Document
from app.models.enums import QaMessageRole, QaMessageStatus
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.summary import SummarySettings
from app.models.user import User
from app.services.search.chunking import rebuild_chunks
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def enable_deterministic() -> None:
    async with get_session_factory()() as s:
        s.add(SummarySettings(enabled=True, provider_type="deterministic"))
        await s.commit()


async def enable_external_openai() -> None:
    from app.services.summary import secrets

    async with get_session_factory()() as s:
        s.add(
            SummarySettings(
                enabled=True, provider_type="openai_compatible",
                endpoint="https://api.example.com/v1", model_name="gpt-x", is_local=False,
            )
        )
        await s.commit()
    secrets.set_api_key("k")


async def setup_doc(client) -> tuple[str, str]:
    res = await client.post(
        "/api/documents", files={"file": ("d.pdf", fx.single_column_korean(pages=2),
                                          "application/pdf")}
    )
    doc_id = res.json()["data"]["id"]
    await drain_jobs()
    async with get_session_factory()() as s:
        await rebuild_chunks(s, uuid.UUID(doc_id))
        await s.commit()
    t = await client.post(f"/api/documents/{doc_id}/qa/threads")
    return doc_id, t.json()["data"]["thread"]["id"]


async def collect(client, doc_id, tid, question="심장은 무엇을 하나요?") -> list[dict]:
    events: list[dict] = []
    async with client.stream(
        "POST", f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
        json={"question": question},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


async def collect_retry(client, doc_id, tid) -> list[dict]:
    events: list[dict] = []
    async with client.stream(
        "POST", f"/api/documents/{doc_id}/qa/threads/{tid}/retry/stream", json={}
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


class TestHappyPath:
    async def test_started_claim_completed_with_sources(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        events = await collect(client, doc_id, tid)
        types = [e["type"] for e in events]
        assert types[0] == "started"
        assert "claim" in types
        assert types[-1] == "completed"
        claim = next(e for e in events if e["type"] == "claim")
        assert claim["sources"][0]["pageNumber"]
        # 내부 chunk id 미노출
        assert "chunkId" not in json.dumps(claim)
        completed = next(e for e in events if e["type"] == "completed")
        assert completed["message"]["status"] == "completed"

    async def test_content_type_ndjson(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        async with client.stream(
            "POST", f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
            json={"question": "심장은 무엇을 하나요?"},
        ) as resp:
            assert "application/x-ndjson" in resp.headers["content-type"]


class TestSourceSafety:
    @pytest.mark.parametrize("hint", ["not_found", "insufficient_evidence"])
    async def test_abstention_clears_draft_claims_from_final_message(
        self, client, monkeypatch, hint
    ):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)

        class AbstainingProvider:
            provider_name = "deterministic"
            model_name = "m"
            available = True
            is_local = True

            def stream_answer(self, request, token):
                chunk = request.chunks[0]
                yield {"type": "claim", "text": chunk.text[:200],
                       "sourceChunkIds": [chunk.chunk_id]}
                yield {"type": "final", "answerStatus": hint,
                       "followUpSuggestions": ["관련된 다른 질문"]}

        monkeypatch.setattr(
            "app.services.qa.stream_service.get_qa_streaming_provider",
            lambda db: _fake_async(AbstainingProvider()),
        )
        events = await collect(client, doc_id, tid)
        assert any(e["type"] == "claim" for e in events)  # 실제 draft 경로를 통과
        message = events[-1]["message"]
        assert message["status"] == hint
        assert message["claims"] == []
        assert "[c0]" not in message["content"]
        async with get_session_factory()() as session:
            saved = await session.get(QaMessage, uuid.UUID(message["id"]))
            assert saved.status.value == hint
            assert saved.content == message["content"]
            assert not saved.followups_json
            assert saved.draft_content is None
            count = await session.scalar(
                select(func.count()).select_from(QaClaim).where(QaClaim.message_id == saved.id)
            )
            assert count == 0

    async def test_unsupported_claim_not_emitted(self, client, monkeypatch):
        # 검색되지 않은/무관한 chunk id를 붙인 claim은 스트림으로 나가지 않는다
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)


        class _Bad:
            provider_name = "deterministic"
            model_name = "m"
            available = True
            is_local = True

            def stream_answer(self, request, token):
                yield {"type": "claim", "text": "가짜", "sourceChunkIds": [str(uuid.uuid4())]}
                yield {"type": "final", "answerStatus": "answered"}

        monkeypatch.setattr(
            "app.services.qa.stream_service.get_qa_streaming_provider",
            lambda db: _fake_async(_Bad()),
        )
        events = await collect(client, doc_id, tid)
        assert not any(e["type"] == "claim" for e in events)
        completed = next(e for e in events if e["type"] == "completed")
        assert completed["message"]["status"] == "insufficient_evidence"


class TestConcurrency:
    async def test_one_stream_per_thread(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        # 진행 중(streaming) assistant 메시지를 직접 삽입 → 새 스트림 시작은 409(JSON)
        async with get_session_factory()() as s:
            s.add(
                QaMessage(
                    thread_id=uuid.UUID(tid),
                    role=QaMessageRole.ASSISTANT,
                    content="",
                    status=QaMessageStatus.STREAMING,
                    sequence_number=1,
                    document_revision=1,
                    stream_request_id="x",
                )
            )
            await s.commit()
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 409


class TestCancel:
    async def test_cancel_during_stream(self, client, monkeypatch):
        # ASGITransport에서 스트림 소비 중 중첩 요청은 교착되므로, 취소 경로는
        # run_stream 제너레이터를 in-process로 구동해 검증한다(HTTP 왕복 없음).
        from app.services.qa import cancel_registry, stream_service

        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        block = threading.Event()

        class _Slow:
            provider_name = "deterministic"
            model_name = "m"
            available = True
            is_local = True

            def stream_answer(self, request, token):
                c = request.chunks[0]
                yield {"type": "claim", "text": c.text[:100], "sourceChunkIds": [c.chunk_id]}
                while not token.is_cancelled():  # 취소될 때까지 응답을 붙잡는다
                    if block.wait(0.05):
                        break

        monkeypatch.setattr(
            stream_service, "get_qa_streaming_provider", lambda db: _fake_async(_Slow())
        )

        async with get_session_factory()() as s:
            user = (await s.execute(select(User))).scalars().first()
            doc = await s.get(Document, uuid.UUID(doc_id))
            thread = await s.get(QaThread, uuid.UUID(tid))
            _u, amsg, rid = await stream_service.prepare_stream(
                s, doc, user, thread, "심장은 무엇을 하나요?"
            )
            aid = amsg.id
            crev, chrev = doc.content_revision, doc.chunk_revision

        events: list[dict] = []
        agen = stream_service.run_stream(
            uuid.UUID(doc_id), uuid.UUID(tid), aid, "심장은 무엇을 하나요?", rid,
            crev, chrev, _FakeRequest(),
        )
        async for ev in agen:
            events.append(ev)
            if ev["type"] == "claim":
                async with get_session_factory()() as s2:
                    m = await s2.get(QaMessage, aid)
                    m.cancel_requested_at = datetime.now(UTC)
                    await s2.commit()
                cancel_registry.request_cancel(str(aid))
            if ev["type"] in ("cancelled", "completed", "error", "interrupted"):
                break
        await agen.aclose()

        assert any(e["type"] == "claim" for e in events)
        assert any(e["type"] == "cancelled" for e in events)
        async with get_session_factory()() as s:
            msg = await s.get(QaMessage, aid)
        assert msg.status == QaMessageStatus.CANCELLED
        # 잠금 해제 — 같은 스레드에서 새 스트림 시작 가능
        block.set()
        started = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert started.status_code == 200

    async def test_cancel_idempotent_on_completed(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        events = await collect(client, doc_id, tid)
        mid = next(e for e in events if e["type"] == "started")["messageId"]
        # 이미 완료 → cancel은 멱등 성공(현재 상태 반환)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/{mid}/cancel"
        )
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "completed"


class TestStatus:
    async def test_status_after_completion(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        events = await collect(client, doc_id, tid)
        mid = next(e for e in events if e["type"] == "started")["messageId"]
        res = await client.get(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/{mid}/status"
        )
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "completed"

    async def test_status_other_thread_404(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        fake_mid = uuid.uuid4()
        res = await client.get(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/{fake_mid}/status"
        )
        assert res.status_code == 404


class TestRevisionGuard:
    async def test_revision_change_discards(self, client, monkeypatch):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)

        from app.services.qa import stream_service

        async def _changed(db, chunk_ids):
            return "CHANGED"

        monkeypatch.setattr(stream_service, "_hash_of_ids", _changed)
        events = await collect(client, doc_id, tid)
        # 저장 직전 해시 변경 감지 → interrupted(REVISION_CHANGED) 이벤트, completed 아님
        assert any(e["type"] == "interrupted" and e["code"] == "REVISION_CHANGED" for e in events)
        assert not any(e["type"] == "completed" for e in events)
        mid = next(e for e in events if e["type"] == "started")["messageId"]
        async with get_session_factory()() as s:
            msg = await s.get(QaMessage, uuid.UUID(mid))
            claims = (
                await s.execute(select(func.count()).select_from(QaClaim).where(
                    QaClaim.message_id == uuid.UUID(mid)
                ))
            ).scalar_one()
        assert msg.status == QaMessageStatus.REVISION_CHANGED
        assert claims == 0
        # user 질문은 보존
        async with get_session_factory()() as s:
            users = (
                await s.execute(select(func.count()).select_from(QaMessage).where(
                    QaMessage.thread_id == uuid.UUID(tid), QaMessage.role == QaMessageRole.USER
                ))
            ).scalar_one()
        assert users == 1


class TestConsent:
    async def test_external_consent_required_before_stream(self, client):
        await enable_external_openai()
        doc_id, tid = await setup_doc(client)
        # 동의 없음 → 스트림 시작 전 403(JSON)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 403


class TestRecovery:
    async def test_interrupted_streams_recovered_on_restart(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        async with get_session_factory()() as s:
            s.add(
                QaMessage(
                    thread_id=uuid.UUID(tid),
                    role=QaMessageRole.ASSISTANT,
                    content="",
                    status=QaMessageStatus.STREAMING,
                    sequence_number=1,
                    document_revision=1,
                    stream_request_id="r1",
                )
            )
            await s.commit()
        from app.services.qa.stream_service import recover_interrupted_streams

        n = await recover_interrupted_streams()
        assert n >= 1
        async with get_session_factory()() as s:
            active = (
                await s.execute(
                    select(func.count()).select_from(QaMessage).where(
                        QaMessage.thread_id == uuid.UUID(tid),
                        QaMessage.status.in_(
                            [QaMessageStatus.STREAMING, QaMessageStatus.PENDING,
                             QaMessageStatus.FINALIZING]
                        ),
                    )
                )
            ).scalar_one()
        assert active == 0

    async def test_recovered_interrupted_answer_can_retry_via_stream(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        async with get_session_factory()() as s:
            doc = await s.get(Document, uuid.UUID(doc_id))
            user_msg = QaMessage(
                thread_id=uuid.UUID(tid),
                role=QaMessageRole.USER,
                content="심장은 무엇을 하나요?",
                status=QaMessageStatus.COMPLETED,
                sequence_number=1,
                document_revision=doc.content_revision,
                chunk_revision=doc.chunk_revision,
            )
            assistant_msg = QaMessage(
                thread_id=uuid.UUID(tid),
                role=QaMessageRole.ASSISTANT,
                content="",
                status=QaMessageStatus.STREAMING,
                sequence_number=2,
                document_revision=doc.content_revision,
                chunk_revision=doc.chunk_revision,
                stream_request_id="before-restart",
                draft_content="부분 답변",
            )
            s.add_all([user_msg, assistant_msg])
            await s.commit()
            assistant_id = assistant_msg.id

        from app.services.qa.stream_service import recover_interrupted_streams

        assert await recover_interrupted_streams() >= 1
        detail = await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")
        interrupted = [
            m for m in detail.json()["data"]["messages"] if m["role"] == "assistant"
        ][-1]
        assert interrupted["status"] == "interrupted"
        assert interrupted["canRetry"] is True

        events = await collect_retry(client, doc_id, tid)
        started = next(e for e in events if e["type"] == "started")
        completed = next(e for e in events if e["type"] == "completed")
        assert started["messageId"] == str(assistant_id)
        assert completed["message"]["status"] in ("completed", "insufficient_evidence")
        assert completed["message"]["canRetry"] is False

        async with get_session_factory()() as s:
            users = (
                await s.execute(
                    select(func.count()).select_from(QaMessage).where(
                        QaMessage.thread_id == uuid.UUID(tid),
                        QaMessage.role == QaMessageRole.USER,
                    )
                )
            ).scalar_one()
            assistants = (
                await s.execute(
                    select(func.count()).select_from(QaMessage).where(
                        QaMessage.thread_id == uuid.UUID(tid),
                        QaMessage.role == QaMessageRole.ASSISTANT,
                    )
                )
            ).scalar_one()
        assert users == 1
        assert assistants == 1


def _fake_async(provider):
    async def _coro():
        return provider

    return _coro()


class TestStreamingCarriesTheNewContract:
    """화면이 쓰는 유일한 경로는 스트리밍이다 — 인용·후속질문·학습수준이 여기서 살아야 한다."""

    async def test_final_content_carries_citation_markers(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        events = await collect(client, doc_id, tid)
        completed = [e for e in events if e["type"] == "completed"][-1]
        msg = completed["message"]
        assert msg["claims"], "지원 주장이 있어야 이 테스트가 의미 있다"
        # 저장된 본문에 마커가 있어야 프론트가 문장 안에서 근거를 짚을 수 있다.
        for i in range(len(msg["claims"])):
            assert f"[c{i}]" in msg["content"]

    async def test_marker_index_matches_claim_order(self, client):
        await enable_deterministic()
        doc_id, tid = await setup_doc(client)
        events = await collect(client, doc_id, tid)
        msg = [e for e in events if e["type"] == "completed"][-1]["message"]
        # [cN]은 claims[N]의 텍스트가 끝나는 자리에 붙는다.
        for i, claim in enumerate(msg["claims"]):
            assert f"{claim['text']}[c{i}]" in msg["content"]

    async def test_followups_from_the_final_event_are_persisted(self, client, monkeypatch):
        await enable_deterministic()
        from app.services.qa import streaming as st

        original = st.DeterministicStreamingQaProvider.stream_answer

        def with_followups(self, request, cancel_token):
            for event in original(self, request, cancel_token):
                if event.get("type") == "final":
                    event = {**event, "followUpSuggestions": ["더 자세히?", "다른 예시는?"]}
                yield event

        monkeypatch.setattr(st.DeterministicStreamingQaProvider, "stream_answer", with_followups)
        doc_id, tid = await setup_doc(client)
        await collect(client, doc_id, tid)
        detail = (await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")).json()["data"]
        assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
        assert assistant["followups"] == ["더 자세히?", "다른 예시는?"]

    async def test_learner_level_reaches_the_streaming_prompt(self, client, monkeypatch):
        await enable_deterministic()
        from app.services.qa import streaming as st

        seen: list[str] = []
        original = st.DeterministicStreamingQaProvider.stream_answer

        def capture(self, request, cancel_token):
            seen.append(request.learner_level)
            yield from original(self, request, cancel_token)

        monkeypatch.setattr(st.DeterministicStreamingQaProvider, "stream_answer", capture)
        doc_id, tid = await setup_doc(client)
        async with client.stream(
            "POST", f"/api/documents/{doc_id}/qa/threads/{tid}/messages/stream",
            json={"question": "심장은 무엇을 하나요?", "learnerLevel": "concise"},
        ) as resp:
            async for _ in resp.aiter_lines():
                pass
        assert seen == ["concise"]

    def test_streaming_prompt_includes_the_level_hint(self):
        from app.services.qa.provider import QaRequest
        from app.services.qa.streaming import _build_user_prompt

        concise = _build_user_prompt(QaRequest(question="q", chunks=[], learner_level="concise"))
        default = _build_user_prompt(QaRequest(question="q", chunks=[]))
        assert concise != default
        assert "간단" in concise
