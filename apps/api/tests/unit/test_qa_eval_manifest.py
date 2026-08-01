"""평가 manifest·fixture 스키마 로드·검증 테스트."""

from pathlib import Path

import pytest

from app.qa_eval import manifest

DATASET = Path(__file__).resolve().parents[1] / "fixtures" / "qa_evaluation"


def test_loads_shipped_dataset():
    ds = manifest.load_dataset(DATASET)
    assert len(ds.cases) >= 8
    assert "doc_heart_ko" in ds.fixtures
    # 모든 케이스의 fixture가 존재
    for c in ds.cases:
        assert c.document_fixture in ds.fixtures


def test_rejects_unknown_category():
    fx = {"d": manifest.Fixture("d", "ko", [["본문"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "nope", "documentFixture": "d",
                        "question": "q", "expectedStatus": "answered"}]},
            fx,
        )


def test_rejects_unknown_fixture():
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "grounded_basic",
                        "documentFixture": "missing", "question": "q",
                        "expectedStatus": "answered"}]},
            {},
        )


def test_rejects_bad_status():
    fx = {"d": manifest.Fixture("d", "ko", [["본문"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "grounded_basic", "documentFixture": "d",
                        "question": "q", "expectedStatus": "maybe"}]},
            fx,
        )


def test_rejects_duplicate_case_id():
    fx = {"d": manifest.Fixture("d", "ko", [["본문"]])}
    case = {"caseId": "dup", "category": "grounded_basic", "documentFixture": "d",
            "question": "q", "expectedStatus": "answered"}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases({"cases": [case, dict(case)]}, fx)


def test_rejects_empty_fixture_pages():
    with pytest.raises(manifest.ManifestError):
        manifest.parse_fixtures({"d": {"language": "ko", "pages": []}})
