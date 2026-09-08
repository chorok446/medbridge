"""Layer 2 preflight — 실제 Ollama·모델 설치 확인. 자동 다운로드하지 않고 명확히 skip한다."""

from __future__ import annotations

import asyncio
import re
import time

from app.services.local_ai import client
from app.services.local_ai import settings as local_st
from app.services.summary.endpoint import SummaryNetworkError, get_json


class ModelNotAllowedError(ValueError):
    """allowlist(qwen3:4b/8b/14b/30b-a3b) 밖 모델."""


class ModelDigestError(RuntimeError):
    """출시 평가에 쓸 모델 digest가 없거나 정규형이 아님."""


_BARE_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CANONICAL_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RELEASE_DIGEST_MAX_ATTEMPTS = 4
_RELEASE_DIGEST_ATTEMPT_TIMEOUT_SEC = 1.0
_RELEASE_DIGEST_TRANSIENT = frozenset({"timeout", "connect_failed"})


def normalize_model_digest(raw: str | None) -> str:
    """Ollama transport의 bare SHA-256을 출시 artifact 정규형으로 바꾼다."""
    if not isinstance(raw, str):
        raise ModelDigestError("설치 모델 digest를 확인할 수 없습니다.")
    if _BARE_DIGEST_RE.fullmatch(raw):
        return f"sha256:{raw}"
    if _CANONICAL_DIGEST_RE.fullmatch(raw):
        return raw
    raise ModelDigestError("설치 모델 digest가 정규 SHA-256 형식이 아닙니다.")


async def release_model_digest(model: str) -> str:
    """현재 tag가 가리키는 manifest digest를 fail-closed로 반환한다."""
    if model not in local_st.ALLOWED_MODELS:
        raise ModelNotAllowedError(
            f"허용되지 않은 모델: {model} (허용: {sorted(local_st.ALLOWED_MODELS)})"
        )
    deadline = time.monotonic() + local_st.STATUS_TIMEOUT_SEC
    for attempt in range(_RELEASE_DIGEST_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            installed = await asyncio.to_thread(
                client.list_models,
                timeout=min(_RELEASE_DIGEST_ATTEMPT_TIMEOUT_SEC, remaining),
            )
        except SummaryNetworkError as exc:
            if (
                exc.category not in _RELEASE_DIGEST_TRANSIENT
                or attempt + 1 >= _RELEASE_DIGEST_MAX_ATTEMPTS
            ):
                raise ModelDigestError("설치 모델 digest를 확인할 수 없습니다.") from exc
            delay = min(0.05 * (2**attempt), max(0.0, deadline - time.monotonic()))
            if delay <= 0:
                break
            await asyncio.sleep(delay)
            continue
        matches = [item for item in installed if item.name == model]
        if len(matches) != 1:
            raise ModelDigestError("출시 모델 tag는 정확히 하나만 설치되어 있어야 합니다.")
        return normalize_model_digest(matches[0].digest)
    raise ModelDigestError("설치 모델 digest 조회 시간이 초과되었습니다.")


async def loaded_release_model_digest(model: str) -> str:
    """고정 loopback `/api/ps`에서 실제 적재 중인 모델 digest를 확인한다."""
    if model not in local_st.ALLOWED_MODELS:
        raise ModelNotAllowedError(
            f"허용되지 않은 모델: {model} (허용: {sorted(local_st.ALLOWED_MODELS)})"
        )
    deadline = time.monotonic() + local_st.STATUS_TIMEOUT_SEC
    for attempt in range(_RELEASE_DIGEST_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.to_thread(
                get_json,
                f"{local_st.OLLAMA_BASE}/api/ps",
                is_local=True,
                timeout=min(_RELEASE_DIGEST_ATTEMPT_TIMEOUT_SEC, remaining),
                max_response_bytes=local_st.STATUS_MAX_BYTES,
            )
        except SummaryNetworkError as exc:
            if (
                exc.category not in _RELEASE_DIGEST_TRANSIENT
                or attempt + 1 >= _RELEASE_DIGEST_MAX_ATTEMPTS
            ):
                raise ModelDigestError("적재 모델 digest를 확인할 수 없습니다.") from exc
            delay = min(0.05 * (2**attempt), max(0.0, deadline - time.monotonic()))
            if delay <= 0:
                break
            await asyncio.sleep(delay)
            continue
        raw_models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw_models, list):
            raise ModelDigestError("적재 모델 목록 응답 형식이 올바르지 않습니다.")
        matches = [
            item
            for item in raw_models
            if isinstance(item, dict) and (item.get("name") or item.get("model")) == model
        ]
        if len(matches) != 1:
            raise ModelDigestError("출시 모델은 적재 목록에 정확히 하나만 있어야 합니다.")
        raw_digest = matches[0].get("digest")
        return normalize_model_digest(raw_digest if isinstance(raw_digest, str) else None)
    raise ModelDigestError("적재 모델 digest 조회 시간이 초과되었습니다.")


async def preflight(model: str) -> tuple[bool, str]:
    """(실행 가능 여부, skip 사유). 127.0.0.1의 Ollama만 사용한다.

    `client.get_status`·`list_models`는 동기 urllib 호출이라 이벤트 루프를 막는다.
    코드베이스의 다른 모든 호출부(routes/local_ai.py, local_ai/service.py)는 예외
    없이 `asyncio.to_thread`로 감싸는데 여기만 그 규칙에서 벗어나 있었다. 지금은
    단일 코루틴 CLI에서만 불려 피해가 없지만, 이 함수가 라우트로 재사용되는 순간
    요청 하나가 최대 STATUS_TIMEOUT_SEC×2 동안 서버 전체를 세운다.
    """
    if model not in local_st.ALLOWED_MODELS:
        raise ModelNotAllowedError(
            f"허용되지 않은 모델: {model} (허용: {sorted(local_st.ALLOWED_MODELS)})"
        )
    status = await asyncio.to_thread(client.get_status)
    if status.status != client.STATUS_READY:
        return False, "Ollama가 실행 중이 아닙니다. 로컬 AI를 실행한 뒤 다시 시도하세요."
    installed = {m.name for m in await asyncio.to_thread(client.list_models)}
    if model not in installed:
        return False, f"모델이 설치되지 않았습니다: {model} (평가는 자동 다운로드하지 않습니다)."
    return True, ""
