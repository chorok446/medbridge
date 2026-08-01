"""Layer 2 preflight — 실제 Ollama·모델 설치 확인. 자동 다운로드하지 않고 명확히 skip한다."""

from __future__ import annotations

from app.services.local_ai import client
from app.services.local_ai import settings as local_st


class ModelNotAllowedError(ValueError):
    """allowlist(qwen3:4b/8b/14b) 밖 모델."""


def preflight(model: str) -> tuple[bool, str]:
    """(실행 가능 여부, skip 사유). 127.0.0.1의 Ollama만 사용한다."""
    if model not in local_st.ALLOWED_MODELS:
        raise ModelNotAllowedError(
            f"허용되지 않은 모델: {model} (허용: {sorted(local_st.ALLOWED_MODELS)})"
        )
    status = client.get_status()
    if status.status != client.STATUS_READY:
        return False, "Ollama가 실행 중이 아닙니다. 로컬 AI를 실행한 뒤 다시 시도하세요."
    installed = {m.name for m in client.list_models()}
    if model not in installed:
        return False, f"모델이 설치되지 않았습니다: {model} (평가는 자동 다운로드하지 않습니다)."
    return True, ""
