"""업로드 상태 머신. 상태 변경은 반드시 이 모듈의 transition()을 거친다."""

from app.core.errors import AppError, ErrorCode
from app.models.document import Document
from app.models.enums import ProcessingStatus as S

_EXTRACTION_DONE = frozenset(
    {S.EXTRACTED, S.PARTIALLY_EXTRACTED, S.OCR_REQUIRED, S.EXTRACTION_FAILED}
)

ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.UPLOADING, S.FAILED}),
    S.UPLOADING: frozenset({S.UPLOADED, S.FAILED}),
    S.UPLOADED: frozenset({S.QUEUED, S.FAILED}),
    S.QUEUED: frozenset({S.VALIDATING, S.FAILED}),
    S.VALIDATING: frozenset({S.READY, S.FAILED}),
    # 검증 완료 → 추출 시작 (취소 시 EXTRACTING → READY 복귀)
    S.READY: frozenset({S.EXTRACTING, S.DELETING}),
    S.EXTRACTING: frozenset(_EXTRACTION_DONE | {S.READY}),
    # 추출 결과 상태들: 재처리(→extracting) 또는 삭제 가능
    S.EXTRACTED: frozenset({S.EXTRACTING, S.DELETING}),
    S.PARTIALLY_EXTRACTED: frozenset({S.EXTRACTING, S.DELETING}),
    S.OCR_REQUIRED: frozenset({S.EXTRACTING, S.DELETING}),
    S.EXTRACTION_FAILED: frozenset({S.EXTRACTING, S.DELETING}),
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
