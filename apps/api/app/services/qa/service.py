"""Q&A 서비스 — 스레드 CRUD + 질문→검색→답변→검증→저장(동기).

동시성: pending assistant 부분 유니크 인덱스로 스레드당 답변 1개(체크-후-삽입 금지).
revision: 시작 스냅샷과 저장 직전 값이 같을 때만 답변을 저장한다.
개인정보: 질문·청크·답변 원문을 로그에 남기지 않는다.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document
from app.models.enums import QaMessageRole, QaMessageStatus
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.user import User
from app.services.qa import context as qa_context
from app.services.qa.factory import get_qa_provider
from app.services.qa.provider import QaHistoryTurn, QaRequest
from app.services.qa.schema import verify
from app.services.qa.settings import (
    HISTORY_MAX_CHARS,
    HISTORY_MAX_TURNS,
    MAX_QUESTION_CHARS,
)
from app.services.summary.service import (
    compute_chunk_hash,
    ensure_external_consent,
    provider_is_external,
)

logger = get_logger(__name__)


# --- 스레드 CRUD ---


async def create_thread(db: AsyncSession, doc: Document, user: User) -> QaThread:
    thread = QaThread(document_id=doc.id, user_id=user.id)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def list_threads(db: AsyncSession, doc: Document) -> list[QaThread]:
    return list(
        (
            await db.execute(
                select(QaThread)
                .where(QaThread.document_id == doc.id)
                .order_by(QaThread.updated_at.desc())
            )
        ).scalars()
    )


async def get_owned_thread(
    db: AsyncSession, doc: Document, thread_id: uuid.UUID
) -> QaThread:
    thread = await db.get(QaThread, thread_id)
    # 다른 문서·사용자의 스레드 존재를 노출하지 않고 404
    if thread is None or thread.document_id != doc.id:
        raise AppError(ErrorCode.NOT_FOUND, "대화를 찾을 수 없습니다.", status_code=404)
    return thread


async def get_thread_messages(
    db: AsyncSession, thread: QaThread
) -> list[tuple[QaMessage, list[QaClaim]]]:
    messages = list(
        (
            await db.execute(
                select(QaMessage)
                .where(QaMessage.thread_id == thread.id)
                .order_by(QaMessage.sequence_number)
            )
        ).scalars()
    )
    msg_ids = [m.id for m in messages]
    claims_by_msg: dict[uuid.UUID, list[QaClaim]] = {}
    if msg_ids:
        claims = (
            await db.execute(
                select(QaClaim)
                .where(QaClaim.message_id.in_(msg_ids))
                .order_by(QaClaim.claim_index)
            )
        ).scalars()
        for c in claims:
            claims_by_msg.setdefault(c.message_id, []).append(c)
    return [(m, claims_by_msg.get(m.id, [])) for m in messages]


async def update_thread(
    db: AsyncSession, thread: QaThread, *, title: str | None, archived: bool | None
) -> QaThread:
    if title is not None:
        thread.title = title.strip()[:300] or None
    if archived is not None:
        thread.archived_at = datetime.now(UTC) if archived else None
    await db.commit()
    await db.refresh(thread)
    return thread


async def delete_thread(db: AsyncSession, thread: QaThread) -> None:
    # 메시지·claim 명시 삭제(스레드 CASCADE FK가 있으나 순서 보장 위해 명시)
    msg_ids = (
        await db.execute(select(QaMessage.id).where(QaMessage.thread_id == thread.id))
    ).scalars().all()
    from sqlalchemy import delete as sa_delete

    if msg_ids:
        await db.execute(sa_delete(QaClaim).where(QaClaim.message_id.in_(msg_ids)))
    await db.execute(sa_delete(QaMessage).where(QaMessage.thread_id == thread.id))
    await db.execute(sa_delete(QaThread).where(QaThread.id == thread.id))
    await db.commit()


# --- 질문 → 답변 ---


@dataclass
class AnswerOutcome:
    user_message: QaMessage | None
    assistant_message: QaMessage
    claims: list[QaClaim]


async def _next_sequence(db: AsyncSession, thread_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(QaMessage.sequence_number)).where(
                QaMessage.thread_id == thread_id
            )
        )
    ).scalar_one_or_none()
    return (current or 0) + 1


async def _recent_history(
    db: AsyncSession, thread_id: uuid.UUID, before_seq: int | None = None
) -> list[QaHistoryTurn]:
    # before_seq를 주면 그 이전 턴만 반환 — 지금 답변 중인 질문이 문맥에 중복으로
    # 들어가지 않게 한다(현재 user 질문은 QaRequest.question으로 이미 전달됨).
    conds = [
        QaMessage.thread_id == thread_id,
        QaMessage.status.in_(
            [QaMessageStatus.COMPLETED, QaMessageStatus.CONFLICTING_EVIDENCE]
        )
        | (QaMessage.role == QaMessageRole.USER),
    ]
    if before_seq is not None:
        conds.append(QaMessage.sequence_number < before_seq)
    rows = list(
        (
            await db.execute(
                select(QaMessage)
                .where(*conds)
                .order_by(QaMessage.sequence_number.desc())
                .limit(HISTORY_MAX_TURNS * 2)
            )
        ).scalars()
    )
    rows.reverse()
    turns: list[QaHistoryTurn] = []
    for m in rows:
        turns.append(QaHistoryTurn(role=m.role.value, content=m.content[:HISTORY_MAX_CHARS]))
    return turns


def _validate_question(question: str) -> str:
    q = (question or "").strip()
    if not q:
        raise AppError(ErrorCode.VALIDATION_FAILED, "질문을 입력해 주세요.", status_code=422)
    if len(q) > MAX_QUESTION_CHARS:
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "질문이 너무 깁니다.", status_code=422
        )
    if any(ord(c) < 0x20 and c not in "\n\t" for c in q):  # 제어문자 거부
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "질문에 허용되지 않는 문자가 있습니다.",
            status_code=422,
        )
    return q


async def ask(
    db: AsyncSession, doc: Document, user: User, thread: QaThread, question: str
) -> AnswerOutcome:
    """질문 저장 → 검색 → 답변 생성 → 검증 → 저장. 동기 응답."""
    q = _validate_question(question)

    provider = await get_qa_provider(db)
    if not provider.available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "질문 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.",
            status_code=501,
        )
    if provider_is_external(provider):
        ensure_external_consent(user, doc)

    # 시작 revision 스냅샷
    start_content_rev = doc.content_revision
    start_chunk_rev = doc.chunk_revision

    # user 메시지 + pending assistant 메시지를 원자적으로 삽입.
    # pending assistant 부분 유니크 인덱스로 스레드 동시 질문을 DB에서 차단한다.
    seq = await _next_sequence(db, thread.id)
    user_msg = QaMessage(
        thread_id=thread.id,
        role=QaMessageRole.USER,
        content=q,
        status=QaMessageStatus.COMPLETED,
        sequence_number=seq,
        document_revision=start_content_rev,
        chunk_revision=start_chunk_rev,
    )
    assistant_msg = QaMessage(
        thread_id=thread.id,
        role=QaMessageRole.ASSISTANT,
        content="",
        status=QaMessageStatus.PENDING,
        sequence_number=seq + 1,
        document_revision=start_content_rev,
        chunk_revision=start_chunk_rev,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
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

    history = await _recent_history(db, thread.id, before_seq=user_msg.sequence_number)
    return await _generate(
        db, doc, user, thread, user_msg, assistant_msg, q, history,
        start_content_rev, start_chunk_rev, provider,
    )


async def retry_last(
    db: AsyncSession, doc: Document, user: User, thread: QaThread
) -> AnswerOutcome:
    """마지막 실패/변경 assistant 메시지를 재시도한다(직전 user 질문으로)."""
    messages = list(
        (
            await db.execute(
                select(QaMessage)
                .where(QaMessage.thread_id == thread.id)
                .order_by(QaMessage.sequence_number.desc())
            )
        ).scalars()
    )
    last_assistant = next((m for m in messages if m.role == QaMessageRole.ASSISTANT), None)
    if last_assistant is None or last_assistant.status not in (
        QaMessageStatus.FAILED,
        QaMessageStatus.REVISION_CHANGED,
    ):
        raise AppError(
            ErrorCode.INVALID_STATE, "다시 시도할 답변이 없습니다.", status_code=409
        )
    # 직전 user 질문 찾기
    last_user = next(
        (m for m in messages if m.role == QaMessageRole.USER
         and m.sequence_number < last_assistant.sequence_number),
        None,
    )
    if last_user is None:
        raise AppError(ErrorCode.INVALID_STATE, "다시 시도할 질문이 없습니다.", status_code=409)

    provider = await get_qa_provider(db)
    if not provider.available:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "질문 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요.",
            status_code=501,
        )
    if provider_is_external(provider):
        ensure_external_consent(user, doc)

    # 실패한 assistant 메시지를 pending으로 되돌린다(부분 유니크 인덱스가 동시 재시도를 막는다).
    start_content_rev = doc.content_revision
    start_chunk_rev = doc.chunk_revision
    last_assistant.status = QaMessageStatus.PENDING
    last_assistant.content = ""
    last_assistant.error_code = None
    last_assistant.document_revision = start_content_rev
    last_assistant.chunk_revision = start_chunk_rev
    last_assistant.provider_name = provider.provider_name
    last_assistant.model_name = provider.model_name
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(
            ErrorCode.INVALID_STATE,
            "이 대화에서 답변을 만드는 중입니다. 잠시 후 다시 시도해 주세요.",
            status_code=409,
        ) from exc

    history = await _recent_history(db, thread.id, before_seq=last_user.sequence_number)
    return await _generate(
        db, doc, user, thread, last_user, last_assistant, last_user.content, history,
        start_content_rev, start_chunk_rev, provider,
    )


async def _fresh_document(db: AsyncSession, document_id: uuid.UUID) -> Document | None:
    """identity map의 낡은 속성이 아니라 DB의 현재 값을 읽는다."""
    return (
        await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


async def _fresh_user(db: AsyncSession) -> User | None:
    return (
        await db.execute(select(User).limit(1).execution_options(populate_existing=True))
    ).scalars().first()


async def _hash_of_ids(db: AsyncSession, chunk_ids: list[str]) -> str:
    """지정한 chunk id들의 현재 (id, content_hash)로 해시를 만든다. 삭제된 id는 빠지므로
    시작 시점 해시와 자동으로 달라진다(retrieved subset 기준 revision guard)."""
    from app.models.search import DocumentChunk

    if not chunk_ids:
        return compute_chunk_hash([])
    uuids = [uuid.UUID(c) for c in chunk_ids]
    rows = (
        await db.execute(
            select(DocumentChunk.id, DocumentChunk.content_hash).where(
                DocumentChunk.id.in_(uuids)
            )
        )
    ).all()
    return compute_chunk_hash([(str(r.id), r.content_hash) for r in rows])


async def _document_changed(
    db: AsyncSession, document_id: uuid.UUID, start_content_rev: int, start_chunk_rev: int | None
) -> bool:
    fresh = await _fresh_document(db, document_id)
    return (
        fresh is None
        or fresh.deleted_at is not None
        or fresh.content_revision != start_content_rev
        or fresh.chunk_revision != start_chunk_rev
    )


async def _generate(
    db, doc, user, thread, user_msg, assistant_msg, question, history,
    start_content_rev, start_chunk_rev, provider,
) -> AnswerOutcome:
    import asyncio

    correlation = None
    try:
        from app.core.logging import correlation_id_var

        correlation = correlation_id_var.get()
    except Exception:
        pass

    # 검색 (현재 문서 범위)
    retrieval = await qa_context.retrieve(db, doc.id, question)

    if not retrieval.chunks:
        # 검색 결과 없음 → 모델 호출하지 않음. 단 저장 직전 문서 상태는 확인한다.
        if await _document_changed(db, doc.id, start_content_rev, start_chunk_rev):
            return await _fail(
                db, assistant_msg, "REVISION_CHANGED", QaMessageStatus.REVISION_CHANGED,
                message="문서 내용이 변경되어 답변을 다시 만들어야 합니다.",
            )
        return await _finalize(
            db, assistant_msg,
            answer="이 자료에서는 확인할 수 없습니다.",
            status=QaMessageStatus.NOT_FOUND,
            retrieval_mode=retrieval.retrieval_mode,
            claims=[],
            user_msg=user_msg,
        )

    request = QaRequest(question=question, chunks=retrieval.chunks, history=history)
    try:
        # 외부 전송 직전 동의 재확인 — identity map의 낡은 값을 피하려고 새로 읽는다.
        if provider_is_external(provider):
            fresh_doc = await _fresh_document(db, doc.id)
            fresh_user = await _fresh_user(db)
            if fresh_doc is None or fresh_user is None or not (
                fresh_doc.external_evidence_enabled and fresh_user.external_ai_allowed
            ):
                return await _fail(db, assistant_msg, "EXTERNAL_CONSENT_MISSING")
        model_output = await asyncio.to_thread(provider.answer, request)
        # 잘못된 최상위 타입(배열·문자열 등)도 여기서 잡아 pending 고착을 막는다
        verified = verify(model_output, retrieval.lookup, had_results=bool(retrieval.chunks))
    except Exception as exc:
        logger.warning(
            "qa_generate_failed",
            document_id=str(doc.id),
            thread_id=str(thread.id),
            result_count=len(retrieval.chunks),
            retrieval_mode=retrieval.retrieval_mode,
            error=type(exc).__name__,
        )
        return await _fail(db, assistant_msg, "QA_FAILED")

    # revision-guarded 저장: 문서 revision + "검색에 실제로 쓴 청크"의 해시가 시작과
    # 같을 때만 저장한다(전체 문서 해시가 아니라 retrieved subset 기준 — 문서에 다른
    # 청크가 더 있어도 정상 저장돼야 한다).
    fresh_doc = await _fresh_document(db, doc.id)
    current_hash = await _hash_of_ids(db, retrieval.searched_ids)
    start_hash = compute_chunk_hash(retrieval.chunk_hash_pairs)
    if (
        fresh_doc is None
        or fresh_doc.deleted_at is not None
        or fresh_doc.content_revision != start_content_rev
        or fresh_doc.chunk_revision != start_chunk_rev
        or current_hash != start_hash
    ):
        return await _fail(db, assistant_msg, "REVISION_CHANGED", QaMessageStatus.REVISION_CHANGED,
                           message="문서 내용이 변경되어 답변을 다시 만들어야 합니다.")

    status_map = {
        "completed": QaMessageStatus.COMPLETED,
        "not_found": QaMessageStatus.NOT_FOUND,
        "insufficient_evidence": QaMessageStatus.INSUFFICIENT_EVIDENCE,
        "conflicting_evidence": QaMessageStatus.CONFLICTING_EVIDENCE,
    }
    claim_rows = [
        QaClaim(
            message_id=assistant_msg.id,
            claim_index=c.claim_index,
            claim_text=c.text,
            verification_status=c.verification_status,
            source_chunk_ids_json=c.source_chunk_ids,
            source_refs_json=c.source_refs,
        )
        for c in verified.claims
    ]
    # 근거가 없거나 부족하면 모델의 자유 서술을 그대로 보여주지 않는다(날조 노출 방지).
    if verified.answer_status in ("not_found", "insufficient_evidence"):
        safe_answer = (
            "이 자료에서는 확인할 수 없습니다."
            if verified.answer_status == "not_found"
            else "문서에서 충분한 근거를 찾지 못했어요. 다른 표현으로 다시 물어봐 주세요."
        )
    else:
        safe_answer = verified.answer or "이 자료에서는 확인할 수 없습니다."
    outcome = await _finalize(
        db, assistant_msg,
        answer=safe_answer,
        status=status_map.get(verified.answer_status, QaMessageStatus.INSUFFICIENT_EVIDENCE),
        retrieval_mode=retrieval.retrieval_mode,
        claims=claim_rows,
        user_msg=user_msg,
        followups=verified.followups,
    )
    logger.info(
        "qa_answered",
        document_id=str(doc.id),
        thread_id=str(thread.id),
        result_count=len(retrieval.chunks),
        retrieval_mode=retrieval.retrieval_mode,
        status=verified.answer_status,
        claims=len(claim_rows),
        correlation_id=correlation,
    )
    return outcome


async def _finalize(
    db, assistant_msg, *, answer, status, retrieval_mode, claims, user_msg, followups=None,
) -> AnswerOutcome:
    assistant_msg.content = answer
    assistant_msg.status = status
    assistant_msg.retrieval_mode = retrieval_mode
    assistant_msg.completed_at = datetime.now(UTC)
    assistant_msg.followups_json = list(followups) if followups else None
    for c in claims:
        db.add(c)
    # 스레드 제목이 없으면 첫 질문으로 자동 지정
    thread = await db.get(QaThread, assistant_msg.thread_id)
    if thread is not None:
        thread.updated_at = datetime.now(UTC)
        if not thread.title and user_msg is not None:
            thread.title = user_msg.content[:60]
    await db.commit()
    await db.refresh(assistant_msg)
    return AnswerOutcome(user_message=user_msg, assistant_message=assistant_msg, claims=claims)


async def _fail(
    db, assistant_msg, code, status=QaMessageStatus.FAILED, message=None
) -> AnswerOutcome:
    assistant_msg.status = status
    assistant_msg.error_code = code
    assistant_msg.completed_at = datetime.now(UTC)
    if message:
        assistant_msg.content = message
    await db.commit()
    await db.refresh(assistant_msg)
    return AnswerOutcome(user_message=None, assistant_message=assistant_msg, claims=[])
