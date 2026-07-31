"""문서 Q&A API — 스레드 CRUD + 질문/재시도(동기 JSON, 스트리밍 없음).

기술 정보(chunk id·모델명·bbox 숫자·raw score)는 응답에 노출하지 않는다. 출처는
사용자 네비게이션용 source_refs(page/bbox)만 내려준다.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.qa import QaClaim, QaMessage, QaThread
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.services.documents.service import get_owned_document
from app.services.qa import service as qa_service
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


class ThreadDetailOut(CamelModel):
    thread: ThreadOut
    messages: list[MessageOut]


def _thread_out(t: QaThread) -> ThreadOut:
    return ThreadOut(
        id=t.id,
        title=t.title,
        archived=t.archived_at is not None,
        created_at=t.created_at.isoformat(),
        updated_at=t.updated_at.isoformat(),
    )


def _claim_out(c: QaClaim) -> ClaimOut:
    return ClaimOut(
        text=c.claim_text,
        verification_status=c.verification_status.value,
        source_refs=c.source_refs_json,
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
    await qa_service.ask(db, doc, user, thread, body.question)
    return wrap(await _answer_detail(db, thread))


@router.post(
    "/{document_id}/qa/threads/{thread_id}/retry",
    response_model=Envelope[ThreadDetailOut],
)
async def retry_message(
    document_id: uuid.UUID,
    thread_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await get_owned_document(db, user, document_id)
    thread = await qa_service.get_owned_thread(db, doc, thread_id)
    await qa_service.retry_last(db, doc, user, thread)
    return wrap(await _answer_detail(db, thread))
