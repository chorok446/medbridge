from typing import Any


class ErrorCode:
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_FILE_TYPE = "INVALID_FILE_TYPE"
    INVALID_PDF_SIGNATURE = "INVALID_PDF_SIGNATURE"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    CORRUPTED_PDF = "CORRUPTED_PDF"
    EMPTY_PDF = "EMPTY_PDF"
    STORAGE_UPLOAD_FAILED = "STORAGE_UPLOAD_FAILED"
    STORAGE_DELETE_FAILED = "STORAGE_DELETE_FAILED"
    DUPLICATE_DOCUMENT = "DUPLICATE_DOCUMENT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    QUEUE_ENQUEUE_FAILED = "QUEUE_ENQUEUE_FAILED"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_STATE = "INVALID_STATE"
    EXTERNAL_AI_OVERWRITE_REQUIRED = "EXTERNAL_AI_OVERWRITE_REQUIRED"
    DUPLICATE_SETTINGS = "DUPLICATE_SETTINGS"
    DB_LOCKED = "DB_LOCKED"
    POST_COMMIT_VIEW_FAILED = "POST_COMMIT_VIEW_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """사용자에게 안전하게 노출 가능한 오류. 내부 예외 메시지는 담지 않는다."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details
