"""업로드 상태 머신. 상태 변경은 반드시 이 모듈의 transition()을 거친다."""

from app.core.errors import AppError, ErrorCode
from app.models.document import Document
from app.models.enums import ProcessingStatus as S

ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.UPLOADING, S.FAILED}),
    S.UPLOADING: frozenset({S.UPLOADED, S.FAILED}),
    S.UPLOADED: frozenset({S.QUEUED, S.FAILED}),
    S.QUEUED: frozenset({S.VALIDATING, S.FAILED}),
    S.VALIDATING: frozenset({S.READY, S.FAILED}),
    S.READY: frozenset({S.DELETING}),
    S.FAILED: frozenset({S.QUEUED, S.DELETING}),
    S.DELETING: frozenset({S.DELETED}),  # 객체 삭제 실패 시 deleting 유지 (전이 없음)
    S.DELETED: frozenset(),
}


def can_transition(current: S, new: S) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(document: Document, new_status: S) -> None:
    """허용된 전이만 수행. 위반 시 AppError(INVALID_STATE)."""
    current = document.processing_status
    if not can_transition(current, new_status):
        raise AppError(
            ErrorCode.INVALID_STATE,
            f"'{current.value}' 상태에서 '{new_status.value}' 상태로 변경할 수 없습니다.",
            status_code=409,
        )
    document.processing_status = new_status
