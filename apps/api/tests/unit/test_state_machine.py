import pytest

from app.core.errors import AppError, ErrorCode
from app.models.document import Document
from app.models.enums import ProcessingStatus as S
from app.services.documents.state_machine import can_transition, transition


def make_doc(status: S) -> Document:
    return Document(processing_status=status)


class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "current,new",
        [
            (S.CREATED, S.UPLOADING),
            (S.UPLOADING, S.UPLOADED),
            (S.UPLOADED, S.QUEUED),
            (S.QUEUED, S.VALIDATING),
            (S.VALIDATING, S.READY),
            (S.VALIDATING, S.FAILED),
            (S.FAILED, S.QUEUED),
            (S.READY, S.DELETING),
            (S.FAILED, S.DELETING),
            (S.DELETING, S.DELETED),
        ],
    )
    def test_allowed(self, current: S, new: S):
        doc = make_doc(current)
        transition(doc, new)
        assert doc.processing_status == new


class TestForbiddenTransitions:
    @pytest.mark.parametrize(
        "current,new",
        [
            (S.CREATED, S.READY),  # 단계 건너뛰기 금지
            (S.QUEUED, S.READY),
            (S.READY, S.QUEUED),  # 완료 문서 재검증 금지
            (S.DELETED, S.QUEUED),  # 삭제 문서 부활 금지
            (S.DELETED, S.DELETING),
            (S.READY, S.VALIDATING),
            (S.UPLOADING, S.QUEUED),
        ],
    )
    def test_forbidden(self, current: S, new: S):
        doc = make_doc(current)
        with pytest.raises(AppError) as exc:
            transition(doc, new)
        assert exc.value.code == ErrorCode.INVALID_STATE
        assert doc.processing_status == current  # 상태 불변

    def test_deleting_has_no_failed_transition(self):
        """객체 삭제 실패 시 deleting 유지가 규칙 — failed로 가는 전이가 없어야 한다."""
        assert not can_transition(S.DELETING, S.FAILED)
