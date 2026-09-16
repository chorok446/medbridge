"""공통 응답 envelope. 모든 응답은 {data, meta} 또는 {error, meta} 형태."""

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar, overload

from pydantic import BaseModel, ConfigDict, field_serializer
from pydantic.alias_generators import to_camel

T = TypeVar("T")


@overload
def utc_isoformat(dt: datetime) -> str: ...
@overload
def utc_isoformat(dt: None) -> None: ...
def utc_isoformat(dt: datetime | None) -> str | None:
    """naive datetime을 UTC로 간주해 오프셋이 붙은 ISO 문자열로 만든다.

    SQLite(aiosqlite)는 DateTime(timezone=True)여도 naive로 돌려주는데, 오프셋 없는
    문자열을 JS의 new Date()가 로컬 시각으로 해석해 표시 시각이 통째로 밀린다.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

    @field_serializer("*", mode="wrap")
    def _serialize_naive_datetime_as_utc(self, value: Any, handler: Any) -> Any:
        # utc_isoformat과 같은 이유 — DB에서 naive로 돌아온 UTC 시각에 오프셋을 되살린다.
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return handler(value)


class Meta(CamelModel):
    correlation_id: str


class Envelope(CamelModel, Generic[T]):
    data: T
    meta: Meta


class ErrorBody(CamelModel):
    code: str
    message: str
    retryable: bool
    details: Any = None


class ErrorEnvelope(CamelModel):
    error: ErrorBody
    meta: Meta
