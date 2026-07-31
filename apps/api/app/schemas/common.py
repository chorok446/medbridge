"""공통 응답 envelope. 모든 응답은 {data, meta} 또는 {error, meta} 형태."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


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
