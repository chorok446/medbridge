"""NDJSON 스트림 이벤트 프로토콜 (Sprint 4B).

한 줄 = JSON 이벤트 하나. 줄 내부 개행은 JSON escaping으로 안전하다(ensure_ascii=False
+ json.dumps는 \\n을 이스케이프한다). 모든 이벤트에 type이 있고, 순서가 필요한
이벤트는 단조 증가 seq를 쓴다.
"""

import json
from collections.abc import AsyncIterator

# 상한 — 무제한 스트림 방지
MAX_EVENT_BYTES = 64 * 1024  # 이벤트 한 줄 최대
MAX_STREAM_BYTES = 8 * 1024 * 1024  # 총 스트림 최대
HEARTBEAT_INTERVAL_SEC = 10.0

# 스트림을 끝내는 이벤트 — 클라이언트 계약상 반드시 하나가 도착해야 하므로
# 크기 상한으로도 버리면 안 된다(버리면 정상 완료가 CONNECTION_LOST로 표시된다).
TERMINAL_EVENT_TYPES = frozenset({"completed", "cancelled", "interrupted", "error"})

CONTENT_TYPE = "application/x-ndjson; charset=utf-8"


def encode_event(event: dict) -> bytes:
    """이벤트 dict → NDJSON 한 줄(bytes). 줄바꿈은 JSON이 이스케이프한다."""
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def started(request_id: str, message_id: str) -> dict:
    return {"type": "started", "requestId": request_id, "messageId": message_id}


def phase(name: str) -> dict:
    return {"type": "phase", "phase": name}


def claim_event(seq: int, claim_index: int, text: str, sources: list[dict]) -> dict:
    # sources는 UI 이동에 필요한 필드만(chunk id·내부 구조 제외)
    return {
        "type": "claim",
        "seq": seq,
        "claimIndex": claim_index,
        "text": text,
        "sources": sources,
    }


def completed(message: dict) -> dict:
    return {"type": "completed", "message": message}


def cancelled(message_id: str) -> dict:
    return {"type": "cancelled", "messageId": message_id}


def interrupted(code: str, retryable: bool = True) -> dict:
    return {"type": "interrupted", "code": code, "retryable": retryable}


def error_event(code: str, message: str, retryable: bool = True) -> dict:
    return {"type": "error", "code": code, "message": message, "retryable": retryable}


def heartbeat(seq: int) -> dict:
    return {"type": "heartbeat", "seq": seq}


async def bounded(events: AsyncIterator[dict]) -> AsyncIterator[bytes]:
    """이벤트 스트림에 크기 상한을 적용해 NDJSON bytes로 내보낸다.

    terminal 이벤트는 상한과 무관하게 항상 통과한다 — 출처가 많은 completed 한 줄이
    64KB를 넘는다고 버리면 완료된 답변이 클라이언트에 연결 끊김으로 표시된다.
    """
    total = 0
    async for event in events:
        chunk = encode_event(event)
        if event.get("type") in TERMINAL_EVENT_TYPES:
            yield chunk
            continue
        if len(chunk) > MAX_EVENT_BYTES:
            continue  # 개별 이벤트가 상한 초과 → 건너뛴다(폭주 방지)
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            # 총 스트림 상한 초과 → 에러로 마무리(생산자 finally가 메시지를 terminal 확정)
            yield encode_event(error_event("QA_STREAM_TOO_LARGE", "답변이 너무 깁니다."))
            return
        yield chunk


def public_source_ref(ref: dict) -> dict:
    """저장된 source_ref → UI로 보낼 안전한 출처(내부 chunk id·DB 구조 제외)."""
    return {
        "pageNumber": ref.get("pageNumber"),
        "bbox": ref.get("bbox"),
        "blockId": ref.get("blockId"),
        "sectionTitle": ref.get("sectionTitle"),
        "sourceMethod": ref.get("sourceMethod"),
    }
