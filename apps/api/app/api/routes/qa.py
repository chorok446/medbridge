"""문서 Q&A API — 스레드 CRUD + 질문/재시도(동기 JSON) + 스트리밍(NDJSON)·취소·상태 조회.

기술 정보(chunk id·모델명·bbox 숫자·raw score)는 응답에 노출하지 않는다. 출처는
사용자 네비게이션용 source_refs(page/bbox)만 내려준다.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.user import User
from app.schemas.common import CamelModel, Envelope, utc_isoformat
from app.services.documents.service import get_owned_document
from app.services.qa import service as qa_service
from app.services.qa import stream_protocol as sp
from app.services.qa import stream_service as qa_stream
from app.utils.responses import wrap

router = APIRouter(prefix="/api/documents", tags=["qa"])


class ThreadOut(CamelModel):
    id: uuid.UUID
    title: str | None
    archived: bool
    created_at: str
    updated_at: str


class ThreadCreateOut(CamelModel):
    thread: ThreadOut


class ThreadUpdate(CamelModel):
    title: str | None = None
    archived: bool | None = None


class QuestionIn(CamelModel):
    question: str = Field(min_length=1)
    # 답변 깊이. 저장하지 않고 요청마다 받는다 — 화면이 마지막 선택을 기억한다.
    learner_level: str = Field(
        default="nursing_student",
        pattern="^(concise|nursing_student|experienced_nurse)$",
    )


class RetryIn(CamelModel):
    learner_level: str = Field(
        default="nursing_student",
        pattern="^(concise|nursing_student|experienced_nurse)$",
    )


class ClaimOut(CamelModel):
    text: str
    verification_status: str
    source_refs: list[dict]


class MessageOut(CamelModel):
    id: uuid.UUID
    role: str
    content: str
    status: str
    sequence_number: int
    retrieval_mode: str | None
    claims: list[ClaimOut]
    # 모델이 제안한 다음 질문. 옛 메시지는 빈 배열로 나간다.
    followups: list[str] = []


class ThreadDetailOut(CamelModel):
    thread: ThreadOut
    messages: list[MessageOut]


def _thread_out(t: QaThread) -> ThreadOut:
    return ThreadOut(
        id=t.id,
        title=t.title,
        archived=t.archived_at is not None,
        created_at=utc_isoformat(t.created_at),
        updated_at=utc_isoformat(t.updated_at),
    )


def _claim_out(c: QaClaim) -> ClaimOut:
    return ClaimOut(
        text=c.claim_text,
        verification_status=c.verification_status.value,
        # 스트림 경로와 동일한 공개 변환 — 내부 chunkId·readingOrder 등을 응답에서 제거한다.
        source_refs=[sp.public_source_ref(r) for r in c.source_refs_json],
    )


def _message_out(m: QaMessage, claims: list[QaClaim]) -> MessageOut:
    return MessageOut(
        id=m.id,
        role=m.role.value,
        content=m.content,
        status=m.status.value,
        sequence_number=m.sequence_number,
        retrieval_mode=m.retrieval_mode,
        claims=[_claim_out(c) for c in claims],
        followups=list(m.followups_json or []),
    )


@router.post("/{document_id}/qa/threads", response_model=Envelope[ThreadCreateOut], status_code=201)
async def create_thread(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.create_thread(db, doc, user)
    return wrap(ThreadCreateOut(thread=_thread_out(thread)))


@router.get("/{document_id}/qa/threads", response_model=Envelope[list[ThreadOut]])
async def list_threads(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    threads = await qa_service.list_threads(db, doc)
    return wrap([_thread_out(t) for t in threads])


@router.get(
    "/{document_id}/qa/threads/{thread_id}", response_model=Envelope[ThreadDetailOut]
)
async def get_thread(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    pairs = await qa_service.get_thread_messages(db, thread)
    return wrap(
        ThreadDetailOut(
            thread=_thread_out(thread),
            messages=[_message_out(m, claims) for m, claims in pairs],
        )
    )


@router.patch(
    "/{document_id}/qa/threads/{thread_id}", response_model=Envelope[ThreadOut]
)
async def patch_thread(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    body: ThreadUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    thread = await qa_service.update_thread(
        db, thread, title=body.title, archived=body.archived
    )
    return wrap(_thread_out(thread))


@router.delete(
    "/{document_id}/qa/threads/{thread_id}", response_model=Envelope[ThreadOut]
)
async def delete_thread(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    out = _thread_out(thread)
    await qa_service.delete_thread(db, thread)
    return wrap(out)


async def _answer_detail(db: AsyncSession, thread: QaThread) -> ThreadDetailOut:
    pairs = await qa_service.get_thread_messages(db, thread)
    return ThreadDetailOut(
        thread=_thread_out(thread),
        messages=[_message_out(m, claims) for m, claims in pairs],
    )


@router.post(
    "/{document_id}/qa/threads/{thread_id}/messages",
    response_model=Envelope[ThreadDetailOut],
)
async def post_message(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    body: QuestionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    await qa_service.ask(
        db, doc, user, thread, body.question, learner_level=body.learner_level
    )
    return wrap(await _answer_detail(db, thread))


@router.post(
    "/{document_id}/qa/threads/{thread_id}/retry",
    response_model=Envelope[ThreadDetailOut],
)
async def retry_message(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    body: RetryIn | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    # 수준은 저장하지 않으므로 재시도 때도 화면이 현재 선택값을 다시 보낸다.
    await qa_service.retry_last(
        db, doc, user, thread, learner_level=(body.learner_level if body else "nursing_student")
    )
    return wrap(await _answer_detail(db, thread))


# --- Sprint 4B: 스트리밍 ---


class MessageStatusOut(CamelModel):
    id: uuid.UUID
    status: str
    error_code: str | None


@router.post("/{document_id}/qa/threads/{thread_id}/messages/stream")
async def stream_message(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    body: QuestionIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    # 스트림 시작 전 검증 오류는 여기서 AppError(JSON)로 반환된다.
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    user_msg, assistant_msg, request_id = await qa_stream.prepare_stream(
        db, doc, user, thread, body.question
    )
    learner_level = body.learner_level
    aid = assistant_msg.id
    user_content = user_msg.content
    start_content_rev = doc.content_revision
    start_chunk_rev = doc.chunk_revision

    events = qa_stream.run_stream(
        document_id, thread_id, aid, user_content, request_id,
        start_content_rev, start_chunk_rev, request,
        learner_level=learner_level,
    )
    return StreamingResponse(
        sp.bounded(events),
        media_type=sp.CONTENT_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{document_id}/qa/threads/{thread_id}/messages/{message_id}/cancel",
    response_model=Envelope[MessageStatusOut],
)
async def cancel_message(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    msg = await qa_stream.request_cancel(db, thread, message_id)
    return wrap(
        MessageStatusOut(id=msg.id, status=msg.status.value, error_code=msg.error_code)
    )


@router.get(
    "/{document_id}/qa/threads/{thread_id}/messages/{message_id}/status",
    response_model=Envelope[MessageStatusOut],
)
async def message_status(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    msg = await qa_stream.get_message_status(db, thread, message_id)
    return wrap(
        MessageStatusOut(id=msg.id, status=msg.status.value, error_code=msg.error_code)
    )
