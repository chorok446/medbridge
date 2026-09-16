"""로컬 AI(Ollama) 온보딩 API — 감지·모델 조회·다운로드·연결 테스트·활성화.

프런트는 Ollama를 직접 호출하지 않는다(프런트 → sidecar → Ollama). 주소는 loopback
고정, 오류 원문·기술 상세는 노출하지 않는다. pull만 NDJSON 스트림, 나머지는 envelope JSON.
"""

import asyncio
import json
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import CamelModel, Envelope
from app.services.local_ai import client, pull_registry
from app.services.local_ai import service as local_service
from app.services.local_ai import settings as st
from app.services.summary.endpoint import SummaryNetworkError
from app.utils.responses import wrap

router = APIRouter(prefix="/api/local-ai", tags=["local-ai"])

_NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"
_POLL_SEC = 0.5
_QUEUE_MAX = 256  # 스레드→async 브리지 backpressure 상한(메모리 폭주 방지)
_MAX_EVENTS = 200_000  # 진행 이벤트 개수 상한(무한 스트림 방지)
_MSG_TOO_LONG = "다운로드 상태가 비정상적으로 길어졌어요."
_MSG_INCOMPLETE = "다운로드가 완료되지 않았어요. 다시 시도해 주세요."


# --- 응답 스키마 ---
class StatusOut(CamelModel):
    status: str  # ready | not_running | incompatible | error


class ModelOut(CamelModel):
    model: str  # 내부명(상세 보기 전용)
    tier: str
    label: str
    description: str
    approx_bytes: int
    installed: bool
    recommended: bool
    ram_advice: str
    disk_ok: bool
    required_bytes: int


class ModelsOut(CamelModel):
    models: list[ModelOut]
    default_model: str
    total_ram_bytes: int | None
    free_disk_bytes: int | None


class TestIn(CamelModel):
    model: str


class TestOut(CamelModel):
    ok: bool
    message: str


class ActivateIn(CamelModel):
    model: str
    overwrite_external: bool = False


class ActivatedOut(CamelModel):
    enabled: bool
    provider_type: str
    model_name: str | None
    is_local: bool


# --- 라우트 ---
@router.get("/status", response_model=Envelope[StatusOut])
async def get_status(user: User = Depends(get_current_user)) -> dict:
    # 감지는 블로킹 네트워크 — 이벤트 루프를 막지 않게 스레드로.
    status = await asyncio.to_thread(client.get_status)
    return wrap(StatusOut(status=status.status))


@router.get("/models", response_model=Envelope[ModelsOut])
async def list_models(user: User = Depends(get_current_user)) -> dict:
    try:
        view = await asyncio.to_thread(local_service.build_models_view)
    except SummaryNetworkError as exc:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "로컬 AI가 준비되지 않았습니다. 실행 상태를 확인하고 다시 시도해 주세요.",
            status_code=409,
        ) from exc
    return wrap(
        ModelsOut(
            models=[
                ModelOut(
                    model=m.model,
                    tier=m.tier,
                    label=m.label,
                    description=m.description,
                    approx_bytes=m.approx_bytes,
                    installed=m.installed,
                    recommended=m.recommended,
                    ram_advice=m.ram_advice,
                    disk_ok=m.disk_ok,
                    required_bytes=m.required_bytes,
                )
                for m in view.models
            ],
            default_model=view.default_model,
            total_ram_bytes=view.total_ram_bytes,
            free_disk_bytes=view.free_disk_bytes,
        )
    )


@router.post("/test", response_model=Envelope[TestOut])
async def test_model(body: TestIn, user: User = Depends(get_current_user)) -> dict:
    if body.model not in st.ALLOWED_MODELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)
    ok, message = await asyncio.to_thread(client.test_model, body.model)
    return wrap(TestOut(ok=ok, message=message))


@router.post("/activate", response_model=Envelope[ActivatedOut])
async def activate(
    body: ActivateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.model not in st.ALLOWED_MODELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)
    # 실제로 설치된 모델만 기본으로 설정한다 — 없는 모델을 활성화해 요약/Q&A가 깨지는 것을 막는다.
    if not await _model_installed(body.model):
        raise AppError(
            ErrorCode.INVALID_STATE,
            "선택한 모델이 아직 설치되지 않았어요. 먼저 내려받아 주세요.",
            status_code=409,
        )
    view = await local_service.activate(db, body.model, overwrite_external=body.overwrite_external)
    return wrap(
        ActivatedOut(
            enabled=view.enabled,
            provider_type=view.provider_type,
            model_name=view.model_name,
            is_local=view.is_local,
        )
    )


@router.post("/models/pull")
async def pull_model(body: TestIn, request: Request, user: User = Depends(get_current_user)):
    # 스트림 시작 전 검증 — allowlist·상태·중복은 JSON AppError로 응답한다.
    if body.model not in st.ALLOWED_MODELS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "지원하지 않는 모델입니다.", status_code=422)
    status = await asyncio.to_thread(client.get_status)
    if status.status != client.STATUS_READY:
        raise AppError(
            ErrorCode.INVALID_STATE,
            "로컬 AI가 준비되지 않았습니다. 실행 상태를 확인해 주세요.",
            status_code=409,
        )
    if not pull_registry.try_begin(body.model):
        raise AppError(
            ErrorCode.INVALID_STATE,
            "이미 다운로드가 진행 중입니다. 완료된 뒤 다시 시도해 주세요.",
            status_code=409,
        )
    return StreamingResponse(
        _pull_events(body.model, request),
        media_type=_NDJSON_CONTENT_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _pull_events(model: str, request: Request):
    """블로킹 pull 제너레이터를 스레드에서 돌리고 NDJSON 이벤트로 변환해 yield한다.

    - registry 해제는 워커 스레드가 실제로 종료될 때(worker finally)만 한다 — 취소 직후
      곧바로 새 다운로드가 시작돼 동시에 두 개가 도는 것을 막는다.
    - 스레드→async 브리지는 세마포어로 backpressure를 걸어 메모리 폭주를 막는다.
    - success 상태는 실제 설치 목록으로 확인한 뒤에만 completed로 알린다.
    blob/digest/manifest/layer 같은 내부 용어는 노출하지 않는다.
    """
    cancel = threading.Event()
    queue: asyncio.Queue = asyncio.Queue()
    slots = threading.Semaphore(_QUEUE_MAX)  # 미소비 이벤트 상한(backpressure)
    loop = asyncio.get_running_loop()

    def _put(item: tuple) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def _worker() -> None:
        try:
            for event in client.pull_model(model, should_cancel=cancel.is_set):
                # progress 이벤트만 backpressure 대상 — 소비자가 느리면 여기서 대기한다.
                while not slots.acquire(timeout=0.5):
                    if cancel.is_set():
                        return
                _put(("event", event))
        except SummaryNetworkError as exc:
            _put(("error", exc.category))  # terminal 이벤트는 슬롯 없이 항상 전달
        except Exception:  # noqa: BLE001 — 원문 비노출
            _put(("error", "unknown"))
        finally:
            _put(("done", None))
            pull_registry.finish(model)  # 스레드가 진짜 끝날 때만 해제

    threading.Thread(target=_worker, daemon=True).start()
    total = 0
    seen = 0
    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(queue.get(), timeout=_POLL_SEC)
            except TimeoutError:
                if await request.is_disconnected():
                    cancel.set()
                    return
                continue
            if kind == "event":
                slots.release()  # progress 소비 완료 → 슬롯 반환(error/done은 슬롯 미사용)
            if kind == "done":
                return
            if kind == "error":
                yield _encode({"type": "error", "message": _pull_error_message(val)})
                return
            seen += 1
            if seen > _MAX_EVENTS:
                cancel.set()
                yield _encode({"type": "error", "message": _MSG_TOO_LONG})
                return
            public = _public_pull_event(val)
            if public is None:
                continue
            if public.get("type") == "completed":
                # success 보고만으로 완료로 단정하지 않는다 — 실제 설치 목록으로 확인한다.
                installed = await _model_installed(model)
                cancel.set()
                if installed:
                    yield _encode({"type": "completed"})
                else:
                    yield _encode({"type": "error", "message": _MSG_INCOMPLETE})
                return
            chunk = _encode(public)
            total += len(chunk)
            if total > st.PULL_MAX_TOTAL_BYTES:
                cancel.set()
                yield _encode({"type": "error", "message": "다운로드 상태가 너무 길어졌습니다."})
                return
            yield chunk
            if await request.is_disconnected():
                cancel.set()
                return
    finally:
        cancel.set()  # registry 해제는 worker가 종료될 때 수행한다


async def _model_installed(model: str) -> bool:
    try:
        installed = await asyncio.to_thread(client.list_models)
    except SummaryNetworkError:
        return False
    return any(m.name == model for m in installed)


def _encode(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# Ollama pull status → 사용자 친화 단계. 내부 용어(manifest/digest/layer)는 감춘다.
def _phase_label(raw_status: str) -> str:
    s = raw_status.lower()
    if "success" in s:
        return "완료"
    if "writing" in s or "removing" in s:  # writing/removing manifest → 마무리
        return "마무리 중"
    if "manifest" in s or "pulling" in s:
        return "준비 중"
    if "download" in s:
        return "내려받는 중"
    if "verif" in s:
        return "확인 중"
    return "진행 중"


def _public_pull_event(event: dict) -> dict | None:
    """Ollama 진행 dict → 공개 이벤트(진행/완료). 내부 식별자는 제거한다."""
    raw_status = event.get("status")
    if not isinstance(raw_status, str):
        return None
    if raw_status.lower() == "success":
        return {"type": "completed"}
    completed = event.get("completed")
    total = event.get("total")
    out: dict = {"type": "progress", "phase": _phase_label(raw_status)}
    if isinstance(total, int) and total > 0:
        out["total"] = total
        if isinstance(completed, int) and completed >= 0:
            out["completed"] = min(completed, total)
            out["percent"] = round(min(completed, total) / total * 100, 1)
    return out


def _pull_error_message(category: object) -> str:
    if category == "response_too_large":
        return "다운로드 응답이 너무 큽니다."
    if category == "timeout":
        return "다운로드 시간이 초과되었습니다. 네트워크를 확인해 주세요."
    if category in ("connect_failed", "unknown"):
        return "다운로드에 실패했습니다. 로컬 AI 실행 상태와 저장 공간을 확인해 주세요."
    return "다운로드에 실패했습니다. 다시 시도해 주세요."
