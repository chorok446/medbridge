"""Layer 2 preflight — 실제 Ollama·모델 설치 확인. 자동 다운로드하지 않고 명확히 skip한다."""

from __future__ import annotations

import asyncio

from app.services.local_ai import client
from app.services.local_ai import settings as local_st


class ModelNotAllowedError(ValueError):
    """allowlist(qwen3:4b/8b/14b/30b-a3b) 밖 모델."""


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
