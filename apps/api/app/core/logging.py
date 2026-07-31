import logging
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict

# 요청/작업 단위 correlation id. 로그에는 식별자만 남기고 파일 내용·개인정보는 남기지 않는다.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def _add_correlation_id(_logger: object, _name: str, event_dict: EventDict) -> EventDict:
    event_dict["correlation_id"] = correlation_id_var.get()
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            _add_correlation_id,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
