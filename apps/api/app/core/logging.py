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
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # 오류 보고서용 파일 로그 (개인정보·토큰·파일 내용은 애초에 로깅하지 않는다)
    try:
        from app.core.paths import get_path_provider

        provider = get_path_provider()
        provider.logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(provider.logs_dir / "sidecar.log", encoding="utf-8"))
    except Exception:
        pass  # 로그 파일을 못 만들어도 앱은 기동한다
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers, force=True)
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
