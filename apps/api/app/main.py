import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import auth, documents, health, reports
from app.core.errors import AppError, ErrorCode
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.workers.broker import setup_broker

logger = get_logger(__name__)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: object = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details,
            },
            "meta": {"correlationId": correlation_id_var.get()},
        },
    )


def create_app() -> FastAPI:
    configure_logging()
    setup_broker()
    app = FastAPI(title="MedBridge Study API", version="0.1.0")

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        correlation_id_var.set(cid)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(
            exc.status_code, exc.code, exc.message, retryable=exc.retryable, details=exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422,
            ErrorCode.VALIDATION_FAILED,
            "요청 값이 올바르지 않습니다.",
            details=[{"loc": [str(x) for x in e["loc"]], "type": e["type"]} for e in exc.errors()],
        )

    @app.exception_handler(Exception)
    async def internal_handler(_request: Request, exc: Exception) -> JSONResponse:
        # 내부 예외 메시지·스택은 사용자에게 노출하지 않는다
        logger.error("unhandled_error", error_type=type(exc).__name__)
        return _error_response(
            500,
            ErrorCode.INTERNAL_ERROR,
            "서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            retryable=True,
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(reports.router)
    return app


app = create_app()
