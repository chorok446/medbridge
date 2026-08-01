"""요약 artifact 변환의 fail-closed 타입·안전 고지 규칙."""

from app.services.summary.schema import ChunkRef, build_artifacts
from app.services.summary.settings import OVERVIEW_MAX_CHARS


def _lookup() -> dict[str, ChunkRef]:
    return {
        "c1": ChunkRef(
            chunk_id="c1",
            text="문서 본문",
            source_refs=[
                {
                    "pageNumber": 1,
                    "blockId": "b1",
                    "bbox": [0, 0, 10, 10],
                    "readingOrder": 0,
                    "sourceMethod": "digital",
                }
            ],
        )
    }


def test_model_study_caution_is_not_persisted_as_document_evidence():
    artifacts = build_artifacts(
        {
            "studyCautions": [
                {
                    "text": "모델이 안전 고지를 임의로 바꿨다.",
                    "sourceChunkIds": ["c1"],
                }
            ]
        },
        _lookup(),
        learner_level="nursing_student",
    )

    assert artifacts == []


def test_oversized_or_non_string_text_is_rejected_not_coerced_or_sliced():
    for value in ("가" * (OVERVIEW_MAX_CHARS + 1), 123, ["본문"]):
        artifacts = build_artifacts(
            {"overview": {"text": value, "sourceChunkIds": ["c1"]}},
            _lookup(),
            learner_level="nursing_student",
        )
        assert artifacts == []


def test_non_string_source_id_is_not_coerced():
    artifacts = build_artifacts(
        {"overview": {"text": "개요", "sourceChunkIds": [123]}},
        _lookup(),
        learner_level="nursing_student",
    )

    assert artifacts == []
