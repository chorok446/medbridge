"""Q&A 스트리밍 오케스트레이션 — 검증 통과 claim만 emit + 원자적 저장.

핵심 안전 원칙: 사용자에게 보내는 점진 답변은 서버 출처 검증(현재 검색 청크 부분집합·
문서 소유·어휘 연결·수치 원문)을 통과한 주장 단위여야 한다. 미검증 자유 토큰을 그대로
노출하지 않는다. 최종 content는 emit된 supported claim들을 서버가 조립한다.

스트림 중 revision·동의·취소·연결 끊김을 최신 DB로 재확인한다(identity-map 불신).
"""

import asyncio
import contextlib
import threading
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document
from app.models.enums import (
    QA_ACTIVE_STATUSES,
    QaClaimVerification,
    QaMessageRole,
    QaMessageStatus,
)
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.user import User
from app.services.qa import cancel_registry
from app.services.qa import context as qa_context
from app.services.qa import stream_protocol as sp
from app.services.qa.factory import get_qa_streaming_provider
from app.services.qa.provider import QaRequest
from app.services.qa.schema import claims_conflict, classify_claim_event
from app.services.qa.service import (
    _fresh_document,
    _fresh_user,
    _hash_of_ids,
    _next_sequence,
    _recent_history,
    _validate_question,
    compute_chunk_hash,
)
from app.services.qa.settings import REVISION_RECHECK_EVERY_CLAIMS, STREAM_POLL_INTERVAL_SEC
from app.services.summary.service import ensure_external_consent, provider_is_external

logger = get_logger(__name__)


class _CancelToken:
    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def is_cancelled(self) -> bool:
        return self._event.is_set()


async def prepare_stream(
    db: AsyncSession, doc: Document, user: User, thread: QaThread, question: str
) -> tuple[QaMessage, QaMessage, str]:
    """스트림 시작 전 검증·메시지 생성(동기). 실패 시 AppError(JSON). 반환:
    (user_msg, assistant_msg, request_id)."""
    q = _validate_question(question)

    provider = await get_qa_streaming_provider(db)
    if not provider.available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "질문 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.",
            status_code=501,
        )
    if provider_is_external(provider):
        ensure_external_consent(user, doc)

    from sqlalchemy import func

    chunk_count = (
        await db.execute(
            select(func.count()).select_from(qa_context.DocumentChunk).where(
                qa_context.DocumentChunk.document_id == doc.id
            )
        )
    ).scalar_one()
    if chunk_count == 0 or doc.chunk_revision != doc.content_revision:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "먼저 문서 검색 준비(청크 생성)를 완료해 주세요.",
            status_code=409,
        )

    request_id = uuid.uuid4().hex
    seq = await _next_sequence(db, thread.id)
    now = datetime.now(UTC)
    user_msg = QaMessage(
        thread_id=thread.id,
        role=QaMessageRole.USER,
        content=q,
        status=QaMessageStatus.COMPLETED,
        sequence_number=seq,
        document_revision=doc.content_revision,
        chunk_revision=doc.chunk_revision,
    )
    assistant_msg = QaMessage(
        thread_id=thread.id,
        role=QaMessageRole.ASSISTANT,
        content="",
        status=QaMessageStatus.STREAMING,
        sequence_number=seq + 1,
        document_revision=doc.content_revision,
        chunk_revision=doc.chunk_revision,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        stream_request_id=request_id,
        stream_started_at=now,
        stream_updated_at=now,
    )
    db.add(user_msg)
    db.add(assistant_msg)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(
            ErrorCode.INVALID_STATE,
            "이 대화에서 답변을 만드는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=409,
        ) from exc
    await db.refresh(user_msg)
    await db.refresh(assistant_msg)
    return user_msg, assistant_msg, request_id


async def request_cancel(
    db: AsyncSession, thread: QaThread, message_id: uuid.UUID
) -> QaMessage:
    """진행 중 스트림에 취소를 요청한다(멱등). DB 플래그 + 인메모리 event."""
    msg = await db.get(QaMessage, message_id)
    if msg is None or msg.thread_id != thread.id or msg.role != QaMessageRole.ASSISTANT:
        raise AppError(ErrorCode.NOT_FOUND, "메시지를 찾을 수 없습니다.", status_code=404)
    if msg.status not in QA_ACTIVE_STATUSES:
        return msg  # 이미 terminal(취소·완료 등) — 멱등 성공
    if msg.cancel_requested_at is None:
        msg.cancel_requested_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(msg)
    cancel_registry.request_cancel(str(message_id))
    return msg


async def get_message_status(
    db: AsyncSession, thread: QaThread, message_id: uuid.UUID
) -> QaMessage:
    msg = await db.get(QaMessage, message_id)
    if msg is None or msg.thread_id != thread.id:
        raise AppError(ErrorCode.NOT_FOUND, "메시지를 찾을 수 없습니다.", status_code=404)
    return msg


async def recover_interrupted_streams() -> int:
    """앱 시작 시 이전 프로세스의 활성 스트림(pending/streaming/finalizing)을 interrupted로
    확정한다. user 질문은 보존, draft는 최종으로 승격하지 않는다. 반환: 복구 건수."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                select(QaMessage).where(
                    QaMessage.role == QaMessageRole.ASSISTANT,
                    QaMessage.status.in_(QA_ACTIVE_STATUSES),
                )
            )
        ).scalars().all()
        for msg in rows:
            msg.status = QaMessageStatus.INTERRUPTED
            msg.error_code = "APP_RESTARTED"
            msg.interrupted_at = datetime.now(UTC)
            msg.completed_at = datetime.now(UTC)
        await s.commit()
    return len(rows)


async def _apply_terminal(
    s: AsyncSession, assistant_id: uuid.UUID, status: QaMessageStatus, *,
    content: str | None = None, error_code: str | None = None,
    claims: list | None = None, clear_draft: bool = True,
) -> bool:
    """주어진 세션에서 활성 상태일 때만 terminal로 원자적 전환하고 (선택) claim 저장.

    조건부 UPDATE(WHERE status IN 활성) + rowcount로 단일 승자를 DB가 판정한다 —
    ORM read-then-write가 아니라 실제 CAS라 동시 확정에도 정확히 한 경로만 이긴다.
    커밋/롤백은 호출부가 한다(검증과 저장을 한 트랜잭션으로 묶기 위함).
    """
    now = datetime.now(UTC)
    values: dict = {"status": status, "completed_at": now}
    if content is not None:
        values["content"] = content
    if error_code is not None:
        values["error_code"] = error_code
    if clear_draft:
        values["draft_content"] = None
    if status == QaMessageStatus.INTERRUPTED:
        values["interrupted_at"] = now
    result = await s.execute(
        update(QaMessage)
        .where(QaMessage.id == assistant_id, QaMessage.status.in_(QA_ACTIVE_STATUSES))
        .values(**values)
    )
    if (cast("CursorResult", result).rowcount or 0) != 1:
        return False  # 이미 다른 경로가 확정함(취소·완료·중단 경쟁)
    if claims:
        for c in claims:
            s.add(
                QaClaim(
                    message_id=assistant_id,
                    claim_index=c.claim_index,
                    claim_text=c.text,
                    verification_status=c.verification_status,
                    source_chunk_ids_json=c.source_chunk_ids,
                    source_refs_json=c.source_refs,
                )
            )
    thread_id = (
        await s.execute(select(QaMessage.thread_id).where(QaMessage.id == assistant_id))
    ).scalar_one_or_none()
    if thread_id is not None:
        await s.execute(update(QaThread).where(QaThread.id == thread_id).values(updated_at=now))
    return True


async def _set_terminal(
    factory, assistant_id: uuid.UUID, status: QaMessageStatus, *,
    content: str | None = None, error_code: str | None = None,
    claims: list | None = None, clear_draft: bool = True,
) -> bool:
    """활성 상태일 때만 terminal로 원자적 CAS 전환. 반환: 이 호출이 이겼는지."""
    async with factory() as s:
        won = await _apply_terminal(
            s, assistant_id, status, content=content, error_code=error_code,
            claims=claims, clear_draft=clear_draft,
        )
        if won:
            await s.commit()
        else:
            await s.rollback()
        return won


async def _message_dto(factory, assistant_id: uuid.UUID) -> dict:
    async with factory() as s:
        msg = await s.get(QaMessage, assistant_id)
        claims = (
            await s.execute(
                select(QaClaim).where(QaClaim.message_id == assistant_id).order_by(
                    QaClaim.claim_index
                )
            )
        ).scalars().all()
    return {
        "id": str(assistant_id),
        "role": "assistant",
        "content": msg.content if msg else "",
        "status": msg.status.value if msg else "failed",
        "claims": [
            {
                "text": c.claim_text,
                "verificationStatus": c.verification_status.value,
                "sourceRefs": [sp.public_source_ref(r) for r in c.source_refs_json],
            }
            for c in claims
        ],
    }


async def _cancel_requested(factory, assistant_id: uuid.UUID) -> bool:
    async with factory() as s:
        msg = await s.get(QaMessage, assistant_id)
        return bool(msg and msg.cancel_requested_at is not None)


async def run_stream(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    assistant_id: uuid.UUID,
    user_content: str,
    request_id: str,
    start_content_rev: int,
    start_chunk_rev: int | None,
    request,
    *,
    learner_level: str = "nursing_student",
    diag: dict | None = None,
):
    """NDJSON 이벤트 dict를 순차 yield하는 async 제너레이터. 항상 assistant를 terminal로 확정.

    diag(선택): 주어지면 모델이 방출한 claim 수·거부 수·거부 사유 코드·final 힌트를 안전한
    분류값으로 기록한다(원문 비노출). 프로덕션은 None으로 두어 계측 비용이 없다(평가 전용).
    """
    from app.db.session import get_session_factory

    factory = get_session_factory()
    cancel_event = cancel_registry.register(str(assistant_id))
    token = _CancelToken(cancel_event)
    supported: list = []
    if diag is not None:
        diag.setdefault("emitted_claims", 0)
        diag.setdefault("rejected_claims", 0)
        diag.setdefault("rejection_reasons", [])
        diag.setdefault("final_hint", "")

    async def _check_broken(s: AsyncSession) -> str | None:
        """주어진 세션에서 revision/동의 변경 확인. 변경 시 사유 문자열, 정상이면 None."""
        doc = await _fresh_document(s, document_id)
        if doc is None or doc.deleted_at is not None:
            return "revision_changed"
        if doc.content_revision != start_content_rev or doc.chunk_revision != start_chunk_rev:
            return "revision_changed"
        if await _hash_of_ids(s, searched_ids) != start_hash:
            return "revision_changed"
        if is_external:
            user = await _fresh_user(s)
            if user is None or not (doc.external_evidence_enabled and user.external_ai_allowed):
                return "consent_revoked"
        return None

    async def _revision_or_consent_broken() -> str | None:
        """변경 시 terminal 상태 문자열 반환, 정상이면 None. 최신 DB 재조회."""
        async with factory() as s:
            return await _check_broken(s)

    try:
        yield sp.started(request_id, str(assistant_id))
        yield sp.phase("retrieving")

        # 검색 + 공급자 구성(설정 세션)
        async with factory() as s:
            retrieval = await qa_context.retrieve(s, document_id, user_content)
            provider = await get_qa_streaming_provider(s)
        if diag is not None:
            diag["retrieved_chunks"] = len(retrieval.chunks)
        is_external = provider_is_external(provider)
        searched_ids = retrieval.searched_ids
        start_hash = compute_chunk_hash(retrieval.chunk_hash_pairs)

        if not retrieval.chunks:
            if (await _revision_or_consent_broken()) == "revision_changed":
                await _set_terminal(factory, assistant_id, QaMessageStatus.REVISION_CHANGED,
                                    content="문서 내용이 변경되어 답변을 다시 만들어야 합니다.",
                                    error_code="REVISION_CHANGED")
                yield sp.interrupted("REVISION_CHANGED")
                return
            await _set_terminal(factory, assistant_id, QaMessageStatus.NOT_FOUND,
                                content="이 자료에서는 확인할 수 없습니다.")
            yield sp.completed(await _message_dto(factory, assistant_id))
            return

        # 외부 호출 직전 동의·revision 재확인
        broken = await _revision_or_consent_broken()
        if broken:
            await _finish_broken(factory, assistant_id, broken)
            yield sp.interrupted(broken.upper())
            return

        yield sp.phase("generating")
        request_obj = QaRequest(
            question=user_content,
            chunks=retrieval.chunks,
            history=await _recent_history_safe(factory, thread_id, assistant_id),
            learner_level=learner_level,
        )

        # 공급자 스트리밍을 스레드에서 실행하고 큐로 넘긴다(네트워크 I/O 격리·취소 가능)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _worker() -> None:
            try:
                for ev in provider.stream_answer(request_obj, token):
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", ev))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", type(exc).__name__))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        threading.Thread(target=_worker, daemon=True).start()

        seq = 0
        claim_index = 0
        polls = 0
        hb = 0
        since_recheck = 0
        stream_final_hint = ""
        hb_every = max(1, int(sp.HEARTBEAT_INTERVAL_SEC / STREAM_POLL_INTERVAL_SEC))
        while True:
            # 짧은 폴링으로 취소·연결 끊김을 빠르게 감지하고, heartbeat는 별도 주기로 낸다
            try:
                kind, val = await asyncio.wait_for(
                    queue.get(), timeout=STREAM_POLL_INTERVAL_SEC
                )
            except TimeoutError:
                if await request.is_disconnected() or cancel_event.is_set():
                    cancel_event.set()
                    st = await _finish_cancel_or_interrupt(factory, assistant_id)
                    yield (
                        sp.cancelled(str(assistant_id))
                        if st == QaMessageStatus.CANCELLED
                        else sp.interrupted("CONNECTION_LOST")
                    )
                    return
                polls += 1
                if polls % hb_every == 0:
                    hb += 1
                    yield sp.heartbeat(hb)
                continue

            if cancel_event.is_set() or await request.is_disconnected():
                cancel_event.set()
                st = await _finish_cancel_or_interrupt(factory, assistant_id)
                yield (sp.cancelled(str(assistant_id))
                       if st == QaMessageStatus.CANCELLED else sp.interrupted("CONNECTION_LOST"))
                return

            if kind == "done":
                break
            if kind == "error":
                cancel_event.set()
                await _set_terminal(
                    factory, assistant_id, QaMessageStatus.FAILED,
                    content="답변을 만들지 못했어요.", error_code="QA_STREAM_FAILED",
                )
                yield sp.error_event("QA_STREAM_FAILED", "답변을 만들지 못했어요.")
                return

            event = val
            if event.get("type") == "final":
                stream_final_hint = str(event.get("answerStatus") or "")
                if diag is not None:
                    diag["final_hint"] = stream_final_hint
                cancel_event.set()  # 공급자 스트림 종료
                break
            if event.get("type") != "claim":
                continue
            if diag is not None:
                diag["emitted_claims"] += 1

            # 주기적으로 revision·동의 재확인 → 변경 시 즉시 중단
            since_recheck += 1
            if since_recheck >= REVISION_RECHECK_EVERY_CLAIMS:
                since_recheck = 0
                broken = await _revision_or_consent_broken()
                if broken:
                    cancel_event.set()
                    await _finish_broken(factory, assistant_id, broken)
                    yield sp.interrupted(broken.upper())
                    return

            vc, reject_reason = classify_claim_event(
                event, retrieval.lookup, claim_index=claim_index
            )
            if vc is None:
                if diag is not None:
                    diag["rejected_claims"] += 1
                    if reject_reason:
                        diag["rejection_reasons"].append(reject_reason)
                continue  # unsupported → 사용자에게 노출하지 않음
            supported.append(vc)
            seq += 1
            yield sp.claim_event(
                seq, claim_index, vc.text, [sp.public_source_ref(r) for r in vc.source_refs]
            )
            claim_index += 1
            await _checkpoint_draft(factory, assistant_id, supported)

        # finalizing
        yield sp.phase("finalizing")
        cancel_event.set()
        if await _cancel_requested(factory, assistant_id) or await request.is_disconnected():
            st = await _finish_cancel_or_interrupt(factory, assistant_id)
            yield (sp.cancelled(str(assistant_id))
                   if st == QaMessageStatus.CANCELLED else sp.interrupted("CONNECTION_LOST"))
            return
        status, content = _final_status(supported, stream_final_hint, had_results=True)
        if status == QaMessageStatus.CONFLICTING_EVIDENCE:
            for c in supported:
                c.verification_status = QaClaimVerification.CONFLICTING
        # revision/동의 재확인과 최종 저장을 한 트랜잭션으로 묶어 TOCTOU 창을 없앤다.
        # ponytail: 단일 트랜잭션이 다중 세션 사이의 틈을 막는다. SQLite의 완전한
        # 직렬성(BEGIN IMMEDIATE)까지는 아니지만 1인 데스크톱 앱엔 충분 — 알려진 상한.
        async with factory() as s:
            broken = await _check_broken(s)
            if broken:
                st_status, st_content, st_code = (
                    (QaMessageStatus.CONSENT_REVOKED,
                     "외부 전송 설정이 변경되어 답변을 중단했습니다.", "CONSENT_REVOKED")
                    if broken == "consent_revoked"
                    else (QaMessageStatus.REVISION_CHANGED,
                          "문서 내용이 변경되어 답변을 다시 만들어야 합니다.", "REVISION_CHANGED")
                )
                await _apply_terminal(
                    s, assistant_id, st_status, content=st_content, error_code=st_code
                )
                await s.commit()
                yield sp.interrupted(broken.upper())
                return
            won = await _apply_terminal(
                s, assistant_id, status, content=content, claims=supported
            )
            if won:
                await s.commit()
            else:
                await s.rollback()
        if not won:
            # 취소/완료 경쟁에서 졌다 — 이미 확정된 terminal 상태를 그대로 반영
            dto = await _message_dto(factory, assistant_id)
            yield (sp.cancelled(str(assistant_id)) if dto["status"] == "cancelled"
                   else sp.completed(dto))
            return
        yield sp.completed(await _message_dto(factory, assistant_id))
    finally:
        # 정상 종료·예외·클라이언트 연결 종료(aclose)로 제너레이터가 어떻게 끝나든
        # (1) worker를 멈추고 (2) 아직 활성이면 반드시 terminal(INTERRUPTED)로 확정한다.
        # _set_terminal은 이미 확정된 경우 no-op이라 정상 완료/취소 경로엔 영향이 없다.
        # aclose가 GeneratorExit을 던져도 정리가 잘리지 않도록 shield로 감싼다.
        cancel_event.set()
        with contextlib.suppress(Exception):
            await asyncio.shield(
                _set_terminal(
                    factory, assistant_id, QaMessageStatus.INTERRUPTED,
                    content="연결이 끊겨 답변이 중단되었습니다.", error_code="CONNECTION_LOST",
                )
            )
        cancel_registry.discard(str(assistant_id))


def _final_status(supported: list, final_hint: str, *, had_results: bool):
    if not supported:
        if not had_results:
            return QaMessageStatus.NOT_FOUND, "이 자료에서는 확인할 수 없습니다."
        return (
            QaMessageStatus.INSUFFICIENT_EVIDENCE,
            "문서에서 충분한 근거를 찾지 못했어요. 다른 표현으로 다시 물어봐 주세요.",
        )
    # 모델이 상충을 명시했거나, final 힌트를 빠뜨렸어도 지원 주장들이 같은 대상에 상반된
    # 극성을 보이면 상충으로 확정한다(상반 근거를 통일된 completed로 노출하지 않는다).
    if len(supported) >= 2 and (
        final_hint == "conflicting_evidence" or claims_conflict([c.text for c in supported])
    ):
        return QaMessageStatus.CONFLICTING_EVIDENCE, "\n".join(c.text for c in supported)
    return QaMessageStatus.COMPLETED, "\n".join(c.text for c in supported)


async def _finish_broken(factory, assistant_id: uuid.UUID, broken: str) -> None:
    if broken == "consent_revoked":
        await _set_terminal(
            factory, assistant_id, QaMessageStatus.CONSENT_REVOKED,
            content="외부 전송 설정이 변경되어 답변을 중단했습니다.",
            error_code="CONSENT_REVOKED",
        )
    else:
        await _set_terminal(
            factory, assistant_id, QaMessageStatus.REVISION_CHANGED,
            content="문서 내용이 변경되어 답변을 다시 만들어야 합니다.",
            error_code="REVISION_CHANGED",
        )


async def _finish_cancel_or_interrupt(factory, assistant_id: uuid.UUID) -> QaMessageStatus:
    """취소 요청이면 cancelled, 아니면(연결 끊김) interrupted로 확정. 확정된 상태 반환."""
    if await _cancel_requested(factory, assistant_id):
        await _set_terminal(factory, assistant_id, QaMessageStatus.CANCELLED,
                            content="답변 생성을 취소했습니다.", error_code="CANCELLED")
        return QaMessageStatus.CANCELLED
    await _set_terminal(factory, assistant_id, QaMessageStatus.INTERRUPTED,
                        content="연결이 끊겨 답변이 중단되었습니다.",
                        error_code="CONNECTION_LOST")
    return QaMessageStatus.INTERRUPTED


async def _checkpoint_draft(factory, assistant_id: uuid.UUID, supported: list) -> None:
    """검증된 claim 텍스트만 draft에 체크포인트(이벤트마다 commit하지 않도록 단순화)."""
    async with factory() as s:
        await s.execute(
            update(QaMessage)
            .where(QaMessage.id == assistant_id, QaMessage.status == QaMessageStatus.STREAMING)
            .values(
                draft_content="\n".join(c.text for c in supported),
                stream_updated_at=datetime.now(UTC),
                last_stream_seq=len(supported),
            )
        )
        await s.commit()


async def _recent_history_safe(factory, thread_id: uuid.UUID, assistant_id: uuid.UUID) -> list:
    async with factory() as s:
        # 현재 (user, assistant) 쌍은 seq(U), seq(U+1)로 삽입된다. before_seq=U로 두어
        # 지금 답변 중인 질문이 문맥에 중복으로 들어가지 않게 한다.
        aseq = (
            await s.execute(
                select(QaMessage.sequence_number).where(QaMessage.id == assistant_id)
            )
        ).scalar_one_or_none()
        before = aseq - 1 if aseq is not None else None
        return await _recent_history(s, thread_id, before_seq=before)
