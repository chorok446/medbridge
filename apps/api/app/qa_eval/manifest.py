"""평가 manifest·fixture 스키마와 로드·검증. 잘못된 케이스는 명확히 거부한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_CATEGORIES = frozenset({
    "grounded_basic",
    "not_found",
    "polarity",
    "numeric",
    "unit",
    "direction",
    "conflict",
    "prompt_injection",
    "long_context",
})

# manifest의 expectedStatus. answered는 서버 상태 completed에 대응한다.
VALID_STATUSES = frozenset({
    "answered",
    "not_found",
    "insufficient_evidence",
    "conflicting_evidence",
})


class ManifestError(ValueError):
    """manifest·fixture 스키마 위반."""


@dataclass(frozen=True)
class Fixture:
    name: str
    language: str
    pages: list[list[str]]  # 페이지 → 블록 텍스트 목록


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    document_fixture: str
    question: str
    expected_status: str
    required_evidence: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    expected_numbers: list[str] = field(default_factory=list)
    expected_units: list[str] = field(default_factory=list)
    expected_polarity: str | None = None  # affirmative | negative
    expected_conflict: bool = False
    maximum_accepted_claims: int | None = None
    safety_critical: bool = False
    notes: str = ""


def _require(cond: object, msg: str) -> None:
    if not cond:
        raise ManifestError(msg)


def _str_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"{where}는 리스트여야 합니다.")
    out = []
    for v in value:
        if not isinstance(v, str):
            raise ManifestError(f"{where} 항목은 문자열이어야 합니다.")
        out.append(v)
    return out


def parse_fixtures(raw: dict) -> dict[str, Fixture]:
    _require(isinstance(raw, dict), "fixtures는 매핑이어야 합니다.")
    out: dict[str, Fixture] = {}
    for name, spec in raw.items():
        _require(isinstance(spec, dict), f"fixture '{name}'는 매핑이어야 합니다.")
        pages = spec.get("pages")
        _require(
            isinstance(pages, list) and len(pages) > 0,
            f"fixture '{name}'에 pages가 필요합니다.",
        )
        norm_pages: list[list[str]] = []
        for pi, page in enumerate(pages):
            _require(isinstance(page, list) and len(page) > 0,
                     f"fixture '{name}' 페이지 {pi}는 블록 리스트여야 합니다.")
            blocks = []
            for block in page:
                _require(isinstance(block, str) and block.strip(),
                         f"fixture '{name}' 페이지 {pi} 블록은 비어있지 않은 문자열이어야 합니다.")
                blocks.append(block)
            norm_pages.append(blocks)
        out[name] = Fixture(
            name=name, language=str(spec.get("language", "ko")), pages=norm_pages
        )
    return out


def parse_cases(raw: dict, fixtures: dict[str, Fixture]) -> list[EvalCase]:
    _require(isinstance(raw, dict) and isinstance(raw.get("cases"), list),
             "manifest에 cases 리스트가 필요합니다.")
    seen: set[str] = set()
    cases: list[EvalCase] = []
    for item in raw["cases"]:
        _require(isinstance(item, dict), "각 case는 매핑이어야 합니다.")
        case_id = item.get("caseId")
        _require(isinstance(case_id, str) and case_id, "caseId가 필요합니다.")
        _require(case_id not in seen, f"중복 caseId: {case_id}")
        seen.add(case_id)
        category = item.get("category")
        _require(category in VALID_CATEGORIES, f"[{case_id}] 알 수 없는 category: {category}")
        fixture = item.get("documentFixture")
        _require(fixture in fixtures, f"[{case_id}] 알 수 없는 documentFixture: {fixture}")
        question = item.get("question")
        _require(
            isinstance(question, str) and question.strip(),
            f"[{case_id}] question이 필요합니다.",
        )
        status = item.get("expectedStatus")
        _require(status in VALID_STATUSES, f"[{case_id}] 알 수 없는 expectedStatus: {status}")
        polarity = item.get("expectedPolarity")
        _require(polarity in (None, "affirmative", "negative"),
                 f"[{case_id}] expectedPolarity는 affirmative|negative여야 합니다.")
        max_claims = item.get("maximumAcceptedClaims")
        _require(max_claims is None or (isinstance(max_claims, int) and max_claims >= 0),
                 f"[{case_id}] maximumAcceptedClaims는 0 이상 정수여야 합니다.")
        cases.append(EvalCase(
            case_id=case_id,
            category=category,
            document_fixture=fixture,
            question=question,
            expected_status=status,
            required_evidence=_str_list(
                item.get("requiredEvidence"), f"[{case_id}] requiredEvidence"
            ),
            forbidden_claims=_str_list(
                item.get("forbiddenClaims"), f"[{case_id}] forbiddenClaims"
            ),
            expected_numbers=_str_list(
                item.get("expectedNumbers"), f"[{case_id}] expectedNumbers"
            ),
            expected_units=_str_list(item.get("expectedUnits"), f"[{case_id}] expectedUnits"),
            expected_polarity=polarity,
            expected_conflict=bool(item.get("expectedConflict", False)),
            maximum_accepted_claims=max_claims,
            safety_critical=bool(item.get("safetyCritical", False)),
            notes=str(item.get("notes", "")),
        ))
    _require(len(cases) > 0, "평가 케이스가 하나 이상 필요합니다.")
    return cases


@dataclass(frozen=True)
class Dataset:
    fixtures: dict[str, Fixture]
    cases: list[EvalCase]


def load_dataset(directory: str | Path) -> Dataset:
    """디렉터리의 fixtures.yaml + manifest.yaml을 로드·검증한다."""
    directory = Path(directory)
    fixtures_raw = yaml.safe_load((directory / "fixtures.yaml").read_text(encoding="utf-8"))
    manifest_raw = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
    fixtures = parse_fixtures(fixtures_raw)
    cases = parse_cases(manifest_raw, fixtures)
    return Dataset(fixtures=fixtures, cases=cases)
