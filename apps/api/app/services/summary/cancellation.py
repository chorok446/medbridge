"""진행 중인 요약 HTTP 요청의 프로세스 내 협력 취소.

DB의 job/run 상태가 영속적인 진실이고, 이 레지스트리는 현재 프로세스에서 blocking
``urllib`` 읽기를 즉시 깨우기 위한 보조 경로다. 신호는 정확한 job/run 쌍마다 분리하며
runner task가 끝나면 반드시 제거한다.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class SummaryCancelled(Exception):
    """사용자 취소·잡 교체로 실행을 조용히 끝내야 한다."""


class SummaryCancellationSignal:
    """thread와 asyncio task가 함께 쓰는 단일 요약 작업 취소 신호."""

    def __init__(self, *, job_id: uuid.UUID, run_id: uuid.UUID) -> None:
        self.job_id = job_id
        self.run_id = run_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._abort_callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """timeout 동안 취소를 기다린다. retry backoff를 즉시 깨우는 데 쓴다."""

        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise SummaryCancelled

    def register_abort(self, callback: Callable[[], None]) -> Callable[[], None]:
        """취소 순간 blocking I/O를 깨울 callback을 등록하고 해제 함수를 반환한다.

        등록과 취소가 경합하면 callback은 정확히 한 번 실행된다. 이미 취소된 신호에는
        callback을 저장하지 않고 즉시 실행해, 늦게 열린 HTTP 응답도 바로 닫히게 한다.
        """

        invoke_now = False
        callback_id: int | None = None
        with self._lock:
            if self._event.is_set():
                invoke_now = True
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._abort_callbacks[callback_id] = callback
        if invoke_now:
            _invoke_abort(callback)

        def unregister() -> None:
            if callback_id is None:
                return
            with self._lock:
                self._abort_callbacks.pop(callback_id, None)

        return unregister

    def cancel(self) -> None:
        """신호를 세우고 현재 등록된 HTTP 응답을 잠금 밖에서 중단한다."""

        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = tuple(self._abort_callbacks.values())
            self._abort_callbacks.clear()
        for callback in callbacks:
            _invoke_abort(callback)


def _invoke_abort(callback: Callable[[], None]) -> None:
    # 취소 전달은 best-effort 보조 경로다. 이미 닫힌 socket 같은 경합 오류 때문에
    # DB에서 확정한 취소 API 자체가 실패해서는 안 된다.
    try:
        callback()
    except Exception:  # noqa: BLE001
        pass


_active_signals: dict[uuid.UUID, set[SummaryCancellationSignal]] = {}
_registry_lock = threading.Lock()
_current_signal: ContextVar[SummaryCancellationSignal | None] = ContextVar(
    "summary_cancellation_signal", default=None
)


def register_summary_cancellation(
    job_id: uuid.UUID, run_id: uuid.UUID
) -> SummaryCancellationSignal:
    signal = SummaryCancellationSignal(job_id=job_id, run_id=run_id)
    with _registry_lock:
        _active_signals.setdefault(job_id, set()).add(signal)
    return signal


def request_summary_cancellation(job_id: uuid.UUID, run_id: uuid.UUID | None = None) -> int:
    """정확한 job(선택 시 run까지 일치)에만 취소를 전달하고 신호 수를 반환한다."""

    with _registry_lock:
        signals = tuple(_active_signals.get(job_id, ()))
    matched = tuple(signal for signal in signals if run_id is None or signal.run_id == run_id)
    for signal in matched:
        signal.cancel()
    return len(matched)


def discard_summary_cancellation(signal: SummaryCancellationSignal) -> None:
    """runner task 완료 시 자신이 등록한 신호만 제거한다."""

    with _registry_lock:
        signals = _active_signals.get(signal.job_id)
        if signals is None:
            return
        signals.discard(signal)
        if not signals:
            _active_signals.pop(signal.job_id, None)


def active_summary_cancellation_count() -> int:
    """레지스트리 누수 검증용 현재 신호 수."""

    with _registry_lock:
        return sum(len(signals) for signals in _active_signals.values())


@contextmanager
def summary_cancellation_scope(
    signal: SummaryCancellationSignal | None,
) -> Iterator[None]:
    """asyncio task와 ``to_thread``가 복사하는 실행별 context를 설치한다."""

    token = _current_signal.set(signal)
    try:
        yield
    finally:
        _current_signal.reset(token)


def current_summary_cancellation() -> SummaryCancellationSignal | None:
    return _current_signal.get()
