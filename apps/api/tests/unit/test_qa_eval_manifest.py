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


def test_quote_expansion_regression_keeps_minimal_source_and_safety_gate():
    ds = manifest.load_dataset(DATASET)
    case = next(c for c in ds.cases if c.case_id == "grounded_quote_without_expansion")
    source = "\n".join(b for page in ds.fixtures[case.document_fixture].pages for b in page)
    assert case.safety_critical
    assert case.expected_status == "answered"
    assert case.expected_numbers == [str(i) for i in range(1, 13)]
    assert {"산소", "영양", "수축", "이완"} <= set(case.forbidden_claims)
    assert all(term not in source for term in case.forbidden_claims)


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


def test_shipped_dataset_has_unit_category():
    ds = manifest.load_dataset(DATASET)
    unit_cases = [c for c in ds.cases if c.category == "unit"]
    assert len(unit_cases) >= 2
    for c in unit_cases:
        assert c.expected_numbers and c.expected_units  # 값-단위 짝
        assert c.safety_critical


def test_rejects_expected_number_absent_in_fixture():
    fx = {"d": manifest.Fixture("d", "ko", [["약물 A의 용량은 5 mg이다"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "unit", "documentFixture": "d",
                        "question": "q", "expectedStatus": "answered",
                        "expectedNumbers": ["9"], "expectedUnits": ["mg"]}]},  # 9는 본문에 없음
            fx,
        )


def test_rejects_expected_unit_absent_in_fixture():
    fx = {"d": manifest.Fixture("d", "ko", [["약물 A의 용량은 5 mg이다"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "unit", "documentFixture": "d",
                        "question": "q", "expectedStatus": "answered",
                        "expectedNumbers": ["5"], "expectedUnits": ["μg"]}]},  # μg는 본문에 없음
            fx,
        )


def test_rejects_unit_case_without_numbers_or_units():
    fx = {"d": manifest.Fixture("d", "ko", [["약물 A의 용량은 5 mg이다"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "unit", "documentFixture": "d",
                        "question": "q", "expectedStatus": "answered"}]},  # 값·단위 없음
            fx,
        )


def test_rejects_unit_case_without_adjacent_pair():
    # 수치와 단위가 본문에 있으나 인접하지 않으면(짝이 아니면) 거부
    fx = {"d": manifest.Fixture("d", "ko", [["수치는 5, 그리고 단위는 별도로 mg"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "unit", "documentFixture": "d",
                        "question": "q", "expectedStatus": "answered",
                        "expectedNumbers": ["5"], "expectedUnits": ["mg"]}]},
            fx,
        )


def test_rejects_conflict_case_without_safety_critical():
    fx = {"d": manifest.Fixture("d", "ko", [["A는 참", "A는 거짓"]])}
    with pytest.raises(manifest.ManifestError):
        manifest.parse_cases(
            {"cases": [{"caseId": "c1", "category": "conflict", "documentFixture": "d",
                        "question": "q", "expectedStatus": "conflicting_evidence",
                        "expectedConflict": True}]},  # safetyCritical 누락
            fx,
        )
