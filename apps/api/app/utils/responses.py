from typing import Any

from app.core.logging import correlation_id_var


def wrap(data: Any) -> dict:
    """모든 성공 응답을 {data, meta.correlationId} envelope로 감싼다."""
    return {"data": data, "meta": {"correlationId": correlation_id_var.get()}}
