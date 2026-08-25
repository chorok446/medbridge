"""Ollama loopback 클라이언트 — 감지·모델 조회·다운로드·연결 테스트.

모든 호출은 고정 loopback 주소로만 나가고 endpoint.py의 안전 HTTP(정책 재검증·redirect
차단·크기/데드라인 상한)를 재사용한다. 오류 원문·stack trace는 노출하지 않는다.
"""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from app.services.local_ai import settings as st
from app.services.model_output import strip_thinking
from app.services.summary.cancellation import SummaryCancellationSignal
from app.services.summary.endpoint import (
    SummaryNetworkError,
    get_json,
    post_json,
    stream_lines,
)

# 상태 리터럴. checking은 프런트 전용(조회 중). loopback 단일 프로브로는 '설치 안 됨'과
# '실행 중 아님'을 구분할 수 없어 not_running으로 통합한다(GUI가 설치 안내+다시 확인으로 커버).
STATUS_READY = "ready"
STATUS_NOT_RUNNING = "not_running"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_ERROR = "error"

_DIGEST_LOOKUP_MAX_ATTEMPTS = 4
_DIGEST_ATTEMPT_TIMEOUT_SEC = 1.0
_DIGEST_RETRY_BASE_SEC = 0.05
_DIGEST_RETRY_CAP_SEC = 0.2
_DIGEST_LOCK_POLL_SEC = 0.025
_DIGEST_TRANSIENT_CATEGORIES = frozenset({"timeout", "connect_failed"})
_DIGEST_LOOKUP_LOCK = threading.Lock()


@dataclass
class OllamaStatus:
    status: str
    version: str | None = None


@dataclass
class InstalledModel:
    name: str
    size: int | None
    parameter_size: str | None
    quantization_level: str | None
    modified_at: str | None
    # Ollama manifest digest. 같은 tag가 다른 모델 내용으로 교체됐는지 판별한다.
    # 맨 뒤 기본값으로 둬 기존 내부 테스트/호출자의 positional 계약을 유지한다.
    digest: str | None = None


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    """'0.6.8', '0.6.8-rc1' 등에서 선행 숫자 3개를 뽑는다. 실패 시 None."""
    import re

    m = re.match(r"(\d+)\.(\d+)\.(\d+)", raw.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def get_status() -> OllamaStatus:
    """GET /api/version → 상태 판정. 연결 실패는 not_running, 그 외 오류는 error."""
    try:
        data = get_json(
            f"{st.OLLAMA_BASE}/api/version",
            is_local=True,
            timeout=st.STATUS_TIMEOUT_SEC,
            max_response_bytes=st.STATUS_MAX_BYTES,
        )
    except SummaryNetworkError as exc:
        # 연결 불가/타임아웃 → 설치 안 됨 또는 실행 중 아님(통합)
        if exc.category in ("connect_failed", "timeout"):
            return OllamaStatus(STATUS_NOT_RUNNING)
        return OllamaStatus(STATUS_ERROR)

    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        return OllamaStatus(STATUS_ERROR)
    parsed = _parse_version(version)
    if parsed is None:
        return OllamaStatus(STATUS_ERROR)
    if parsed < st.MIN_OLLAMA_VERSION:
        return OllamaStatus(STATUS_INCOMPATIBLE, version=version)
    return OllamaStatus(STATUS_READY, version=version)


def list_models(
    *,
    cancellation_signal: SummaryCancellationSignal | None = None,
    timeout: float | None = None,
) -> list[InstalledModel]:
    """GET /api/tags → allowlist 필터 + 내부 필드만 추출. schema 위반은 건너뛴다."""
    data = get_json(
        f"{st.OLLAMA_BASE}/api/tags",
        is_local=True,
        timeout=st.STATUS_TIMEOUT_SEC if timeout is None else timeout,
        max_response_bytes=st.STATUS_MAX_BYTES,
        cancellation_signal=cancellation_signal,
    )
    if not isinstance(data, dict):
        raise SummaryNetworkError("bad_response")
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        raise SummaryNetworkError("bad_response")
    out: list[InstalledModel] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if not isinstance(name, str) or name not in st.ALLOWED_MODELS:
            continue  # allowlist 밖 모델은 무시
        raw_details = item.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
        size = item.get("size")
        out.append(
            InstalledModel(
                name=name,
                size=int(size) if isinstance(size, int) else None,
                parameter_size=_opt_str(details.get("parameter_size")),
                quantization_level=_opt_str(details.get("quantization_level")),
                modified_at=_opt_str(item.get("modified_at")),
                digest=_opt_str(item.get("digest")),
            )
        )
    return out


def installed_model_digest(
    model: str, *, cancellation_signal: SummaryCancellationSignal | None = None
) -> str | None:
    """allowlist 모델 tag의 현재 manifest digest를 제한 시간 안에 조회한다.

    Windows Ollama는 짧은 ``/api/tags`` 연결을 간헐적으로 reset한다. 단일
    transient 실패를 모델 교체로 잘못 판정하면 대형 문서의 병렬 요약 중 어느
    한 guard만 실패해도 전체 run이 중단된다. 전체 상태 조회 예산은 기존
    ``STATUS_TIMEOUT_SEC``를 넘지 않고 transient 범주만 취소 가능한 backoff로
    재시도한다. 스키마 위반과 지속 실패는 기존처럼 None으로 fail-closed된다.
    """
    if model not in st.ALLOWED_MODELS:
        return None

    _acquire_digest_lookup_slot(cancellation_signal)
    try:
        # lock 대기 때문에 후속 caller의 실제 조회 예산이 줄면 정상 catalog도
        # provider 변경으로 오인한다. 각 caller의 4초 예산은 slot 획득 후 시작한다.
        deadline = time.monotonic() + st.STATUS_TIMEOUT_SEC
        for attempt in range(_DIGEST_LOOKUP_MAX_ATTEMPTS):
            if cancellation_signal is not None:
                cancellation_signal.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                models = list_models(
                    cancellation_signal=cancellation_signal,
                    timeout=min(_DIGEST_ATTEMPT_TIMEOUT_SEC, remaining),
                )
            except SummaryNetworkError as exc:
                if (
                    exc.category not in _DIGEST_TRANSIENT_CATEGORIES
                    or attempt + 1 >= _DIGEST_LOOKUP_MAX_ATTEMPTS
                ):
                    return None
                remaining = deadline - time.monotonic()
                delay = min(
                    _DIGEST_RETRY_BASE_SEC * (2**attempt),
                    _DIGEST_RETRY_CAP_SEC,
                    max(0.0, remaining),
                )
                if delay <= 0:
                    return None
                if cancellation_signal is not None:
                    if cancellation_signal.wait(delay):
                        cancellation_signal.raise_if_cancelled()
                else:
                    time.sleep(delay)
                continue
            return next((item.digest for item in models if item.name == model), None)
        return None
    finally:
        _DIGEST_LOOKUP_LOCK.release()


def _acquire_digest_lookup_slot(
    cancellation_signal: SummaryCancellationSignal | None,
) -> None:
    """Ollama의 짧은 catalog 연결이 몰리지 않게 하나씩 실행한다."""

    while True:
        if cancellation_signal is not None:
            cancellation_signal.raise_if_cancelled()
        if _DIGEST_LOOKUP_LOCK.acquire(timeout=_DIGEST_LOCK_POLL_SEC):
            return


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def pull_model(model: str, *, should_cancel: Callable[[], bool] | None = None) -> Iterator[dict]:
    """POST /api/pull (stream=true) → NDJSON 진행 dict yield. model은 allowlist여야 한다.

    blob/digest/manifest/layer 같은 내부 용어는 그대로 노출하지 않고 상위에서 사용자
    친화 이벤트로 변환한다. insecure/registry 지정 없이 이름만 보낸다.
    """
    if model not in st.ALLOWED_MODELS:
        raise SummaryNetworkError("model_not_found")
    import json

    payload = {"model": model, "stream": True}
    for line in stream_lines(
        f"{st.OLLAMA_BASE}/api/pull",
        payload,
        "",  # 로컬 — 인증 헤더 없음
        is_local=True,
        connect_timeout=st.PULL_CONNECT_TIMEOUT_SEC,
        idle_timeout=st.PULL_IDLE_TIMEOUT_SEC,
        total_deadline=st.PULL_TOTAL_DEADLINE_SEC,
        max_line_bytes=st.PULL_MAX_LINE_BYTES,
        max_total_bytes=st.PULL_MAX_TOTAL_BYTES,
        should_cancel=should_cancel,
    ):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def test_model(model: str) -> tuple[bool, str]:
    """짧은 비-문서 프로브로 모델을 확인한다. 문서 원문·질문·개인정보를 보내지 않는다.

    반환 (ok, 사용자 메시지). HTTP 성공만 보지 않고 content 형식·비어있음까지 검증한다.
    """
    if model not in st.ALLOWED_MODELS:
        return False, "선택한 모델을 찾을 수 없습니다. 모델을 다시 선택해 주세요."
    from app.services.summary.settings import LOCAL_REASONING_EFFORT

    payload = {
        "model": model,
        "temperature": 0,
        "reasoning_effort": LOCAL_REASONING_EFFORT,
        "stream": False,
        "max_tokens": st.TEST_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": st.NO_THINK_HINT},
            {"role": "user", "content": "안녕하세요라고 한 문장으로 답하세요."},
        ],
    }
    try:
        data = post_json(
            f"{st.OLLAMA_OPENAI_BASE}/chat/completions",
            payload,
            "",  # 로컬 — 인증 헤더 없음
            is_local=True,
            timeout=st.TEST_TIMEOUT_SEC,
            max_response_bytes=st.TEST_MAX_BYTES,
        )
    except SummaryNetworkError as exc:
        if exc.category == "model_not_found":
            return False, "선택한 모델이 설치되어 있지 않습니다. 먼저 내려받아 주세요."
        if exc.category in ("server_error", "bad_response"):
            # 메모리 부족·모델 로드 실패 등을 안전 문구로 변환
            return False, "로컬 AI가 모델을 불러오지 못했습니다. 저장 공간·메모리를 확인해 주세요."
        return False, exc.user_message

    content = _extract_content(data)
    if content is None:
        return False, "로컬 AI 응답 형식을 확인하지 못했습니다. 다시 시도해 주세요."
    if not strip_thinking(content):
        return False, "로컬 AI가 빈 응답을 보냈습니다. 다른 모델을 선택하거나 다시 시도해 주세요."
    return True, "로컬 AI를 사용할 준비가 됐습니다."


def _extract_content(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    try:
        choices = data["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None
