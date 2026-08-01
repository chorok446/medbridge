"""NDJSON 스트림 이벤트 프로토콜 (Sprint 4B).

한 줄 = JSON 이벤트 하나. 줄 내부 개행은 JSON escaping으로 안전하다(ensure_ascii=False
+ json.dumps는 \\n을 이스케이프한다). 모든 이벤트에 type이 있고, 순서가 필요한
이벤트는 단조 증가 seq를 쓴다.
"""

import json

# 상한 — 무제한 스트림 방지
MAX_EVENT_BYTES = 64 * 1024  # 이벤트 한 줄 최대
MAX_STREAM_BYTES = 8 * 1024 * 1024  # 총 스트림 최대
HEARTBEAT_INTERVAL_SEC = 10.0

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


def public_source_ref(ref: dict) -> dict:
    """저장된 source_ref → UI로 보낼 안전한 출처(내부 chunk id·DB 구조 제외)."""
    return {
        "pageNumber": ref.get("pageNumber"),
        "bbox": ref.get("bbox"),
        "blockId": ref.get("blockId"),
        "sectionTitle": ref.get("sectionTitle"),
        "sourceMethod": ref.get("sourceMethod"),
    }
