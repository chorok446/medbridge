import logging
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict

# 요청/작업 단위 correlation id. 로그에는 식별자만 남기고 파일 내용·개인정보는 남기지 않는다.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

# 화면이 진행 중인 작업을 1~1.5초마다 폴링하는 엔드포인트들. 성공한 폴링은 로그에
# 남기지 않는다 — 실기기 오류 보고서에서 logTail 200줄 중 163줄이 폴링이었고 진단에
# 쓸 수 있는 줄은 1줄뿐이었다. 작업이 몇 분만 돌아도 창이 폴링으로 가득 찬다.
#
# `-status`도 함께 본다. 가장 시끄러운 두 경로가 하이픈이다(`/{id}/ocr-status`,
# `/{id}/extraction-status` — 각각 1초마다 폴링한다). 슬래시 형태만 보면 정작 잡아야
# 할 잡음을 그대로 두게 된다.
_POLL_PATH_SUFFIXES = ("/status", "-status", "/jobs")


class _AccessLogNoiseFilter(logging.Filter):
    """uvicorn 액세스 로그에서 성공한 폴링·프리플라이트를 뺀다.

    실패(4xx·5xx)는 폴링이라도 남긴다 — 그게 진단해야 할 신호다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True  # 형식이 다르면 판단하지 않고 남긴다
        _addr, method, path, _http_version, status = args[:5]
        if not isinstance(status, (int, str)):
            return True
        try:
            status_code = int(status)
        except ValueError:
            return True
        if status_code >= 400:
            return True
        if method == "OPTIONS":
            return False  # CORS 프리플라이트는 진단 가치가 없다
        if method != "GET" or not isinstance(path, str):
            return True
        return not path.split("?", 1)[0].endswith(_POLL_PATH_SUFFIXES)


class _ClientDisconnectFilter(logging.Filter):
    """뷰어가 요청을 끊을 때 나는 ConnectionResetError 트레이스백을 뺀다.

    PDF 뷰어가 렌더 중 요청을 취소하면 Windows에서 WinError 10054로 올라오는데,
    asyncio 기본 핸들러가 5줄짜리 트레이스백으로 남긴다. 정상적인 disconnect이고
    조치할 것이 없는데 로그 꼬리만 잡아먹는다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.exc_info:
            return True
        return not isinstance(record.exc_info[1], ConnectionResetError)


def install_log_noise_filters() -> None:
    """폴링·disconnect 잡음 필터를 붙인다 (여러 번 불러도 한 번만 붙는다).

    핸들러가 아니라 로거에 붙인다 — uvicorn이 나중에 dictConfig로 자기 로거의
    핸들러를 갈아끼워도 로거의 필터는 그대로 남기 때문이다.
    """
    for logger_name, filter_cls in (
        ("uvicorn.access", _AccessLogNoiseFilter),
        ("asyncio", _ClientDisconnectFilter),
    ):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(f, filter_cls) for f in logger.filters):
            logger.addFilter(filter_cls())


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
    install_log_noise_filters()
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            _add_correlation_id,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
