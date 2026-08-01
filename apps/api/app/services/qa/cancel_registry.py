"""진행 중 스트림 취소용 인메모리 레지스트리.

message_id → threading.Event. 취소 API·연결 끊김·revision/동의 변경 감지 시 event를
set하면, 공급자 스레드가 다음 읽기 루프에서 멈추고 응답을 닫는다. DB cancel_requested_at은
프로세스 재시작 후 복구·상태 조회의 영속 기준이다(레지스트리는 현재 프로세스 한정).
"""

import threading

_events: dict[str, threading.Event] = {}
_lock = threading.Lock()


def register(message_id: str) -> threading.Event:
    ev = threading.Event()
    with _lock:
        _events[message_id] = ev
    return ev


def request_cancel(message_id: str) -> bool:
    """해당 메시지의 진행 중 스트림에 취소를 전달한다. 있으면 True."""
    with _lock:
        ev = _events.get(message_id)
    if ev is not None:
        ev.set()
        return True
    return False


def discard(message_id: str) -> None:
    with _lock:
        _events.pop(message_id, None)
