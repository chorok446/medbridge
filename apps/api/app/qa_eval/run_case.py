"""한 평가 케이스를 실제 서비스 경로(prepare_stream + run_stream)로 실행하고 결과를 수집한다.

모델의 chat/completions를 직접 호출하지 않는다 — 검색·컨텍스트·검증·출처 재구성·스트리밍·
저장을 모두 통과한 뒤 사용자에게 실제로 도달한 주장·출처만 평가 대상으로 삼는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.auth import get_or_create_profile
from app.core.errors import AppError
from app.models.document import Document
from app.models.extraction import DocumentBlock
from app.models.qa import QaThread
from app.models.summary import SummarySettings
from app.qa_eval.manifest import EvalCase, Fixture
from app.qa_eval.synthetic import seed_document
from app.services.local_ai import settings as local_st
from app.services.qa import stream_service


@dataclass
class ClaimView:
    text: str
    sources: list[dict] = field(default_factory=list)  # {pageNumber, blockId, bbox, ...}


@dataclass
class CaseRun:
    case_id: str
    category: str
    status: str  # 서버 terminal 상태 (completed/not_found/insufficient_evidence/...)
    terminal_type: str  # completed|cancelled|interrupted|error|none
    claims: list[ClaimView] = field(default_factory=list)
    started: bool = False
    reached_terminal: bool = False
    timed_out: bool = False
    error_code: str | None = None
    latency_sec: float = 0.0
    first_claim_sec: float | None = None
    # 독립 검증용 문서 사실
    owned_block_ids: set[str] = field(default_factory=set)
    doc_text: str = ""
    prestream_error: str | None = None  # prepare_stream 단계 거부(501/403/409 등)
    # 진단(안전 분류값만 — 원문 비노출)
    emitted_claim_count: int = 0  # 모델이 방출한 claim 이벤트 수(검증 전)
    rejected_claim_count: int = 0  # 서버 검증에서 거부된 수
    rejection_reason_codes: list[str] = field(default_factory=list)
    provider_final_hint: str = ""  # 모델이 낸 final answerStatus 힌트(없으면 "")
    retrieved_chunk_count: int = 0  # 검색이 반환한 청크 수(검색 실패 vs 모델 실패 구분용)

    @property
    def provider_error_category(self) -> str | None:
        """스트림/공급자 단계 실패 분류(원문 없이 코드만)."""
        if self.prestream_error is not None:
            return None
        if self.timed_out:
            return "timeout"
        code = self.error_code
        if code is None:
            return None
        return {
            "QA_STREAM_FAILED": "provider_stream_error",
            "QA_FAILED": "provider_error",
            "CONNECTION_LOST": "connection_lost",
            "REVISION_CHANGED": "revision_changed",
            "CONSENT_REVOKED": "consent_revoked",
        }.get(code, "internal_error")

    @property
    def prestream_error_category(self) -> str | None:
        """prepare_stream 단계 거부 분류(AppError 코드는 안전 상수)."""
        return self.prestream_error


class _FakeRequest:
    """연결 유지 요청. 필요 시 disconnect_after 이벤트 수 뒤 끊긴 것으로 만든다."""

    def __init__(self, disconnect_after: int | None = None) -> None:
        self._remaining = disconnect_after
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        if self._remaining is None:
            return False
        self._remaining -= 1
        return self._remaining < 0


async def _set_provider(factory, *, provider_mode: str, model: str | None) -> None:
    async with factory() as s:
        rows = (await s.execute(select(SummarySettings))).scalars().all()
        for row in rows:
            await s.delete(row)
        # 지우기를 먼저 내보낸다. 설정 행은 고정 id(SETTINGS_SINGLETON_ID)를 쓰므로,
        # 같은 flush에 삭제와 삽입이 함께 들어가면 SQLAlchemy가 INSERT를 먼저 보내
        # PK 충돌이 난다.
        await s.flush()
        if provider_mode == "deterministic":
            s.add(SummarySettings(enabled=True, provider_type="deterministic"))
        else:  # local Ollama
            s.add(SummarySettings(
                enabled=True, provider_type="openai_compatible", is_local=True,
                endpoint=local_st.OLLAMA_OPENAI_BASE, model_name=model,
            ))
        await s.commit()


async def run_case(
    factory,
    case: EvalCase,
    fixture: Fixture,
    *,
    provider_mode: str,
    model: str | None,
    timeout_sec: float,
) -> CaseRun:
    """케이스를 실행해 CaseRun을 반환한다. 예외를 던지지 않고 결과로 표현한다."""
    await _set_provider(factory, provider_mode=provider_mode, model=model)

    async with factory() as s:
        user = await get_or_create_profile(s)
        doc_id = await seed_document(s, user.id, fixture)

    # 독립 검증용 문서 사실 수집
    async with factory() as s:
        blocks = (
            await s.execute(select(DocumentBlock).where(DocumentBlock.document_id == doc_id))
        ).scalars().all()
        owned_block_ids = {str(b.id) for b in blocks}
        doc_text = "\n".join(b.text for b in blocks)
        thread_user = await get_or_create_profile(s)
        thread = QaThread(document_id=doc_id, user_id=thread_user.id)
        s.add(thread)
        await s.commit()
        tid = thread.id

    run = CaseRun(
        case_id=case.case_id, category=case.category, status="none", terminal_type="none",
        owned_block_ids=owned_block_ids, doc_text=doc_text,
    )

    # prepare_stream — 스트림 시작 전 거부(모델 미연결·동의·동시성)는 결과로 기록
    async with factory() as s:
        doc = await s.get(Document, doc_id)
        user = await get_or_create_profile(s)
        thread = await s.get(QaThread, tid)
        try:
            _u, assistant_msg, rid = await stream_service.prepare_stream(
                s, doc, user, thread, case.question
            )
        except AppError as exc:
            run.prestream_error = exc.code
            return run
        aid = assistant_msg.id
        crev, chrev = doc.content_revision, doc.chunk_revision

    start = time.monotonic()
    diag: dict = {}
    agen = stream_service.run_stream(
        doc_id, tid, aid, case.question, rid, crev, chrev, _FakeRequest(), diag=diag
    )
    completed_message: dict | None = None
    try:
        async with asyncio.timeout(timeout_sec):
            async for ev in agen:
                etype = ev.get("type")
                if etype == "started":
                    run.started = True
                elif etype == "claim":
                    if run.first_claim_sec is None:
                        run.first_claim_sec = time.monotonic() - start
                    run.claims.append(
                        ClaimView(text=ev.get("text", ""), sources=ev.get("sources", []))
                    )
                elif etype in ("completed", "cancelled", "interrupted", "error"):
                    run.reached_terminal = True
                    run.terminal_type = etype
                    if etype == "completed":
                        completed_message = ev.get("message") or {}
                        run.status = completed_message.get("status", "completed")
                    elif etype == "interrupted":
                        run.status = "interrupted"
                        run.error_code = ev.get("code")
                    elif etype == "cancelled":
                        run.status = "cancelled"
                    else:
                        run.status = "failed"
                        run.error_code = ev.get("code")
    except TimeoutError:
        run.timed_out = True
    except Exception as exc:  # noqa: BLE001 — 러너는 던지지 않고 결과로 표현한다
        run.status = "failed"
        run.error_code = type(exc).__name__
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()  # 모든 경로에서 스트림 정리
        run.latency_sec = time.monotonic() - start
        run.emitted_claim_count = int(diag.get("emitted_claims", 0))
        run.rejected_claim_count = int(diag.get("rejected_claims", 0))
        run.rejection_reason_codes = list(diag.get("rejection_reasons", []))
        run.provider_final_hint = str(diag.get("final_hint", ""))
        run.retrieved_chunk_count = int(diag.get("retrieved_chunks", 0))

    # completed면 최종 message(공개 NDJSON 계약)의 claim/source로 확정 반영 —
    # 저장 경로까지 통과한 결과를 본다(stream_service 내부에 의존하지 않는다).
    if run.terminal_type == "completed" and completed_message is not None:
        run.status = completed_message.get("status", run.status)
        run.claims = [
            ClaimView(text=c.get("text", ""), sources=c.get("sourceRefs", []))
            for c in completed_message.get("claims", [])
        ]
    return run
