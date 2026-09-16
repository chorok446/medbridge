"""평가 전용 관찰 정보. 원문/응답 문자열/예외 메시지/경로를 직렬화하지 않는다."""

from __future__ import annotations

import http.client
import urllib.error
from dataclasses import dataclass, field

from app.services.summary.endpoint import SummaryNetworkError

_ERROR_KINDS = {
    SummaryNetworkError: "SummaryNetworkError",
    urllib.error.URLError: "URLError",
    urllib.error.HTTPError: "HTTPError",
    OSError: "OSError",
    TimeoutError: "TimeoutError",
    ConnectionError: "ConnectionError",
    ConnectionResetError: "ConnectionResetError",
    ConnectionAbortedError: "ConnectionAbortedError",
    ConnectionRefusedError: "ConnectionRefusedError",
    BrokenPipeError: "BrokenPipeError",
    http.client.RemoteDisconnected: "RemoteDisconnected",
}


def exception_chain_codes(exc: BaseException) -> list[dict]:
    """최대 8개 원인/문맥/URLError.reason 예외. 문자열에서 오류 번호를 추측하지 않는다."""
    pending, seen = [exc], set()
    result: list[dict] = []
    while pending and len(result) < 8:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        item: dict = {"kind": _ERROR_KINDS.get(type(current), "other_error")}
        for name in ("errno", "winerror"):
            value = getattr(current, name, None) if isinstance(current, OSError) else None
            item[name] = value if type(value) is int and -65535 <= value <= 65535 else None
        result.append(item)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif not current.__suppress_context__ and current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, urllib.error.URLError) and isinstance(current.reason, BaseException):
            pending.append(current.reason)
    return result


@dataclass
class SelectionStepTrace:
    step: int
    scope: str  # whole / bundle / global_guard
    bundle_indices: tuple[int, ...]  # 지역 응답 번호에 대응하는 전역 묶음 번호
    phase: str = "preflight"
    request_started: bool = False
    status: str | None = None
    assessments: list[dict] = field(default_factory=list)
    conflict: bool | None = None
    error_chain: list[dict] = field(default_factory=list)


@dataclass
class SelectionCallStats:
    """관찰 전용. 중단된 요청도 포함하며 선택/시간/재시도 정책에는 사용하지 않는다."""

    requests_started: int = 0
    capture_steps: bool = False
    steps: list[SelectionStepTrace] = field(default_factory=list)
