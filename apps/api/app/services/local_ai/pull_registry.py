"""진행 중인 모델 다운로드 추적 — 중복 pull 시작을 막는다(단일 사용자, 동시 1개)."""

import threading

_lock = threading.Lock()
_active: set[str] = set()


def try_begin(model: str) -> bool:
    """pull 시작을 등록한다. 이미 진행 중인 pull이 있으면 False."""
    with _lock:
        if _active:  # 어떤 모델이든 하나라도 진행 중이면 새 다운로드를 막는다
            return False
        _active.add(model)
        return True


def finish(model: str) -> None:
    with _lock:
        _active.discard(model)


def is_active() -> bool:
    with _lock:
        return bool(_active)
