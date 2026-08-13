"""문서 Q&A API 통합 테스트 — 스레드 CRUD·소유권·검색·출처·revision·동시성·동의·공급자."""

import uuid

from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.enums import QaMessageRole, QaMessageStatus
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.summary import SummarySettings
from app.services.search.chunking import rebuild_chunks
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs


async def enable_deterministic() -> None:
    async with get_session_factory()() as s:
        s.add(SummarySettings(enabled=True, provider_type="deterministic"))
        await s.commit()


async def enable_external_openai() -> None:
    from app.services.summary import secrets

    async with get_session_factory()() as s:
        s.add(
            SummarySettings(
                enabled=True,
                provider_type="openai_compatible",
                endpoint="https://api.example.com/v1",
                model_name="gpt-x",
                is_local=False,
            )
        )
        await s.commit()
    secrets.set_api_key("test-key")


async def upload_chunked(client, data: bytes) -> str:
    res = await client.post("/api/documents", files={"file": ("d.pdf", data, "application/pdf")})
    assert res.status_code == 201, res.text
    doc_id = res.json()["data"]["id"]
    await drain_jobs()
    async with get_session_factory()() as s:
        await rebuild_chunks(s, uuid.UUID(doc_id))
        await s.commit()
    return doc_id


async def new_thread(client, doc_id: str) -> str:
    res = await client.post(f"/api/documents/{doc_id}/qa/threads")
    assert res.status_code == 201
    return res.json()["data"]["thread"]["id"]


def assistant_of(detail: dict) -> dict:
    return [m for m in detail["messages"] if m["role"] == "assistant"][-1]


class TestThreadCrud:
    async def test_create_list_get_patch_delete(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)

        lst = (await client.get(f"/api/documents/{doc_id}/qa/threads")).json()["data"]
        assert any(t["id"] == tid for t in lst)

        patched = await client.patch(
            f"/api/documents/{doc_id}/qa/threads/{tid}", json={"title": "심장 공부"}
        )
        assert patched.json()["data"]["title"] == "심장 공부"

        archived = await client.patch(
            f"/api/documents/{doc_id}/qa/threads/{tid}", json={"archived": True}
        )
        assert archived.json()["data"]["archived"] is True

        got = await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")
        assert got.status_code == 200

        deleted = await client.delete(f"/api/documents/{doc_id}/qa/threads/{tid}")
        assert deleted.status_code == 200
        assert (await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")).status_code == 404


class TestOwnership:
    async def test_unknown_document_404(self, client):
        fake = uuid.uuid4()
        assert (await client.post(f"/api/documents/{fake}/qa/threads")).status_code == 404

    async def test_thread_from_other_document_404(self, client):
        await enable_deterministic()
        doc_a = await upload_chunked(client, fx.single_column_korean(pages=1))
        doc_b = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_a)
        # doc_b 경로로 doc_a의 thread 접근 → 404
        res = await client.get(f"/api/documents/{doc_b}/qa/threads/{tid}")
        assert res.status_code == 404


class TestAskFlow:
    async def test_answer_with_sources(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 200
        detail = res.json()["data"]
        a = assistant_of(detail)
        assert a["status"] == "completed"
        assert a["retrievalMode"] == "keyword"
        assert a["claims"]
        ref = a["claims"][0]["sourceRefs"][0]
        # 공개 출처는 이동에 필요한 필드만 — 내부 layout 순서(readingOrder)는 노출하지 않는다.
        for key in ("pageNumber", "blockId", "bbox", "sourceMethod"):
            assert key in ref
        assert "readingOrder" not in ref
        assert "chunkId" not in ref

    async def test_no_match_returns_not_found_without_model(self, client, monkeypatch):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        # 공급자가 호출되면 실패시켜 "검색 0 → 모델 미호출"을 검증
        from app.services.qa import provider as prov

        def _boom(self, request):
            raise AssertionError("model should not be called when no results")

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", _boom)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "존재하지않는단어xyz절대없음"},
        )
        a = assistant_of(res.json()["data"])
        assert a["status"] == "not_found"

    async def test_technical_info_not_exposed(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        raw = res.text
        assert "sourceChunkIds" not in raw
        assert "deterministic-qa-v1" not in raw

    async def test_uncited_model_prose_is_not_stored(self, client, monkeypatch):
        """지원 claim 하나가 있어도 인용 없는 자유 산문은 최종 답변에 섞이지 않는다."""
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        from app.services.qa import provider as prov

        emitted: dict[str, str] = {}

        def _mixed_output(self, request):
            claim = request.chunks[0].text[:200].strip()
            emitted["claim"] = claim
            return {
                "answer": f"{claim}[c0]\n인슐린은 모든 사람에게 항상 안전합니다.",
                "answerStatus": "answered",
                "claims": [
                    {"text": claim, "sourceChunkIds": [request.chunks[0].chunk_id]}
                ],
            }

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", _mixed_output)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 200
        assistant = assistant_of(res.json()["data"])
        assert assistant["content"] == f"{emitted['claim']}[c0]"
        assert "인슐린" not in assistant["content"]
        assert len(assistant["claims"]) == 1


class TestValidation:
    async def test_empty_question_422(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages", json={"question": "   "}
        )
        assert res.status_code == 422

    async def test_too_long_question_422(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "가" * 3000},
        )
        assert res.status_code == 422

    async def test_provider_disabled_501(self, client):
        # 요약 모델 미연결 → 501, 앱 전체는 정상
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 501
        # 검색은 여전히 동작
        s = await client.post(
            f"/api/documents/{doc_id}/search", json={"query": "심장은", "mode": "keyword"}
        )
        assert s.status_code == 200


class TestRevisionGuard:
    async def test_revision_change_during_answer_discards(self, client, monkeypatch):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        duuid = uuid.UUID(doc_id)
        tid = await new_thread(client, doc_id)

        # 저장 직전 검색 청크 해시가 시작과 달라진 상황을 모사(중간 재생성 등)
        from app.services.qa import service as qa_service

        async def _changed_hash(db, chunk_ids):
            return "CHANGED-DIFFERENT-HASH"

        monkeypatch.setattr(qa_service, "_hash_of_ids", _changed_hash)
        assert duuid  # 사용됨
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        detail = res.json()["data"]
        a = assistant_of(detail)
        assert a["status"] == "revision_changed"
        # 사용자 질문은 보존된다
        assert any(m["role"] == "user" for m in detail["messages"])
        # claim은 저장되지 않는다
        async with get_session_factory()() as s:
            n = (
                await s.execute(select(func.count()).select_from(QaClaim))
            ).scalar_one()
        assert n == 0


class TestMultiChunkNotDiscarded:
    async def test_answer_saved_when_document_has_more_chunks_than_retrieved(self, client):
        # 검색에 안 걸리는 다른 청크가 문서에 더 있어도 정상 저장돼야 한다.
        # (revision guard는 '검색에 쓴 청크' 기준 — 전체 문서 기준이면 항상 폐기됨)
        import uuid as _uuid

        from app.models.search import DocumentChunk

        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        duuid = uuid.UUID(doc_id)
        # 질문("심장")과 무관한 별도 청크를 추가 → 검색 subset < 전체 문서
        async with get_session_factory()() as s:
            s.add(
                DocumentChunk(
                    id=_uuid.uuid4(),
                    document_id=duuid,
                    chunk_index=999,
                    section_title="무관",
                    normalized_text="간 담즙 소화 해독 기능 설명 문단",
                    token_count=10,
                    page_start=1,
                    page_end=1,
                    source_refs_json=[
                        {"pageNumber": 1, "blockId": "extra", "bbox": [0, 0, 1, 1],
                         "readingOrder": 99, "sourceMethod": "digital"}
                    ],
                    content_hash="extra-distinct-hash",
                )
            )
            await s.commit()

        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        a = assistant_of(res.json()["data"])
        assert a["status"] != "revision_changed"
        assert a["status"] in ("completed", "insufficient_evidence")


class TestMalformedModelOutput:
    async def test_wrong_type_json_marks_failed_not_pending(self, client, monkeypatch):
        # 모델이 유효 JSON이지만 최상위가 dict가 아니면(배열 등) 무한 pending이 아니라 failed
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        from app.services.qa import provider as prov

        def _list_output(self, request):
            return ["not", "a", "dict"]

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", _list_output)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        a = assistant_of(res.json()["data"])
        assert a["status"] == "failed"
        # 스레드가 잠기지 않고 다시 질문할 수 있어야 한다(pending 고착 방지)
        again = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/retry"
        )
        assert again.status_code == 200


class TestConcurrency:
    async def test_pending_answer_blocks_second_question(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        tuuid = uuid.UUID(tid)
        # 진행 중(pending) assistant 메시지를 직접 삽입
        async with get_session_factory()() as s:
            s.add(
                QaMessage(
                    thread_id=tuuid,
                    role=QaMessageRole.ASSISTANT,
                    content="",
                    status=QaMessageStatus.PENDING,
                    sequence_number=1,
                    document_revision=1,
                )
            )
            await s.commit()
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 409


class TestRetry:
    async def test_retry_after_failure(self, client, monkeypatch):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)

        # 첫 시도를 실패시킨다 (원래 구현을 보관했다가 재시도 때 복구)
        from app.services.qa import provider as prov

        orig_answer = prov.DeterministicQaProvider.answer

        def _fail(self, request):
            raise RuntimeError("boom")

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", _fail)
        first = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        failed = assistant_of(first.json()["data"])
        assert failed["status"] == "failed"
        assert failed["canRetry"] is True

        # 공급자 복구 후 재시도
        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", orig_answer)
        res = await client.post(f"/api/documents/{doc_id}/qa/threads/{tid}/retry")
        assert res.status_code == 200
        retried = assistant_of(res.json()["data"])
        assert retried["status"] in ("completed", "insufficient_evidence")
        assert retried["canRetry"] is False


class TestConsent:
    async def test_external_provider_blocked_without_consent(self, client):
        await enable_external_openai()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert res.status_code == 403


class TestForeignSource:
    async def test_model_foreign_chunk_id_rejected(self, client, monkeypatch):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)

        from app.services.qa import provider as prov

        def _foreign(self, request):
            return {
                "answer": "가짜 출처",
                "answerStatus": "answered",
                "claims": [{"text": "가짜", "sourceChunkIds": [str(uuid.uuid4())]}],
            }

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", _foreign)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        a = assistant_of(res.json()["data"])
        # 알 수 없는 chunk id → 지원 주장 0 → insufficient_evidence
        assert a["status"] == "insufficient_evidence"


class TestDeleteCascade:
    async def test_document_delete_removes_qa(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=1))
        tid = await new_thread(client, doc_id)
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 200
        async with get_session_factory()() as s:
            threads = (await s.execute(select(func.count()).select_from(QaThread))).scalar_one()
            msgs = (await s.execute(select(func.count()).select_from(QaMessage))).scalar_one()
            claims = (await s.execute(select(func.count()).select_from(QaClaim))).scalar_one()
        assert threads == 0 and msgs == 0 and claims == 0


class TestFollowupsReachTheClient:
    """모델이 만든 후속 질문은 저장되어 다시 불러도 살아 있어야 한다."""

    async def test_followups_are_persisted_and_returned(self, client, monkeypatch):
        await enable_deterministic()
        from app.services.qa import provider as prov

        original = prov.DeterministicQaProvider.answer

        def with_followups(self, request):
            out = original(self, request)
            out["followUpSuggestions"] = ["더 자세히?", "다른 예시는?"]
            return out

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", with_followups)

        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        # 응답이 아니라 "다시 조회"로 확인한다 — 저장되지 않으면 여기서 사라진다.
        detail = (await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")).json()["data"]
        assert assistant_of(detail)["followups"] == ["더 자세히?", "다른 예시는?"]

    async def test_messages_without_followups_return_empty_list(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        detail = (await client.get(f"/api/documents/{doc_id}/qa/threads/{tid}")).json()["data"]
        assert assistant_of(detail)["followups"] == []


class TestLearnerLevelWiring:
    """라우트 → 서비스 → 공급자까지 여러 홉이라 배선이 끊겨도 조용하다. 끝에서 확인한다."""

    async def test_requested_level_reaches_the_provider(self, client, monkeypatch):
        await enable_deterministic()
        from app.services.qa import provider as prov

        seen: list[str] = []
        original = prov.DeterministicQaProvider.answer

        def capture(self, request):
            seen.append(request.learner_level)
            return original(self, request)

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", capture)

        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?", "learnerLevel": "experienced_nurse"},
        )
        assert seen == ["experienced_nurse"]

    async def test_omitted_level_uses_the_default(self, client, monkeypatch):
        await enable_deterministic()
        from app.services.qa import provider as prov

        seen: list[str] = []
        original = prov.DeterministicQaProvider.answer

        def capture(self, request):
            seen.append(request.learner_level)
            return original(self, request)

        monkeypatch.setattr(prov.DeterministicQaProvider, "answer", capture)

        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?"},
        )
        assert seen == ["nursing_student"]

    async def test_unknown_level_is_rejected_at_the_edge(self, client):
        await enable_deterministic()
        doc_id = await upload_chunked(client, fx.single_column_korean(pages=2))
        tid = await new_thread(client, doc_id)
        res = await client.post(
            f"/api/documents/{doc_id}/qa/threads/{tid}/messages",
            json={"question": "심장은 무엇을 하나요?", "learnerLevel": "wizard"},
        )
        assert res.status_code == 422
