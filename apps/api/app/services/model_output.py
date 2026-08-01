"""모델 출력 후처리 — Qwen3 등의 thinking/reasoning 흔적 제거.

thinking 내용은 사용자 응답·로그·대화 history에 남기지 않는다. API의 비사고 설정이
우선이지만, 모델이 그래도 <think> 블록을 내보내면 여기서 제거한다.
"""

import re

# <think>...</think> (대소문자 무시, 여러 줄). 닫는 태그가 없으면 그 이후를 통째로 버린다.
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_OPEN_THINK = re.compile(r"<think\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)


def strip_thinking(text: str) -> str:
    """content에서 thinking 블록을 제거하고 양끝 공백을 정리한다."""
    if not text:
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _OPEN_THINK.sub("", cleaned)  # 닫히지 않은 <think>는 이후 전부 제거
    return cleaned.strip()
