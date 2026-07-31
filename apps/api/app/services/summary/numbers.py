"""중요 수치·대상 집단의 결정론적 후보 추출.

모델이 아니라 정규식으로 후보를 먼저 수집한다. 최종 수치 문자열이 실제 청크 원문에
존재하는지 반드시 검증한다(모델이 없는 수치를 만들 여지를 두지 않는다). 새로운 임상
판단·진단은 생성하지 않는다 — 원문에 있는 수치를 그대로 인용할 뿐이다.
"""

import re

from app.models.enums import SummaryArtifactType
from app.services.summary.schema import ArtifactDraft, ChunkRef, _refs_for

# 숫자+단위/백분율/연령/기간/용량/기준범위/연구대상수 후보. 한국어·영어 단위 혼용.
_UNITS = r"mg|g|kg|mcg|µg|ug|ml|mL|L|mmHg|bpm|mmol/L|mg/dL|IU|단위"
_NUMBER_PATTERNS = [
    r"\d+(?:\.\d+)?\s?%",  # 백분율
    rf"\d+(?:\.\d+)?\s?(?:{_UNITS})",  # 용량·수치+단위
    r"\d+\s?(?:세|살)",  # 연령
    r"\d+(?:\.\d+)?\s?(?:년|개월|주|일|시간|분)",  # 기간
    r"\d+\s?[-~]\s?\d+(?:\.\d+)?",  # 범위 (10-20)
    r"[Nn]\s?=\s?\d+",  # 연구 대상 수
]
_NUMBER_RE = re.compile("|".join(f"(?:{p})" for p in _NUMBER_PATTERNS))

# 대상 집단 후보 — 포함/제외 기준, 대상 표현이 있는 문장
_POPULATION_HINTS = ("대상", "환자", "포함 기준", "제외 기준", "참여자", "군", "그룹")

_MAX_NUMBERS = 30
_MAX_POPULATIONS = 15


def extract_number_artifacts(
    lookup: dict[str, ChunkRef], *, start_position: int
) -> list[ArtifactDraft]:
    """청크 원문에서 수치를 추출해 IMPORTANT_NUMBER artifact를 만든다."""
    drafts: list[ArtifactDraft] = []
    seen: set[str] = set()
    position = start_position
    for cid, ref in lookup.items():
        for match in _NUMBER_RE.finditer(ref.text):
            value = match.group(0).strip()
            # 실제 원문에 존재하는지 재확인 (검증 요구사항 — regex 매치 자체가 원문 근거)
            if value not in ref.text or value in seen:
                continue
            seen.add(value)
            drafts.append(
                ArtifactDraft(
                    artifact_type=SummaryArtifactType.IMPORTANT_NUMBER,
                    title=value,
                    position=position,
                    content_json={"value": value, "context": _context(ref.text, value)},
                    source_chunk_ids=[cid],
                    source_refs=_refs_for([cid], lookup),
                )
            )
            position += 1
            if len(drafts) >= _MAX_NUMBERS:
                return drafts
    return drafts


def extract_population_artifacts(
    lookup: dict[str, ChunkRef], *, start_position: int
) -> list[ArtifactDraft]:
    """대상 집단 힌트가 있는 문장을 TARGET_POPULATION 후보로 만든다."""
    drafts: list[ArtifactDraft] = []
    seen: set[str] = set()
    position = start_position
    for cid, ref in lookup.items():
        for sentence in re.split(r"(?<=[.!?。\n])\s+", ref.text):
            s = sentence.strip()
            if len(s) < 6 or len(s) > 300:
                continue
            if not any(h in s for h in _POPULATION_HINTS):
                continue
            if s in seen:
                continue
            seen.add(s)
            drafts.append(
                ArtifactDraft(
                    artifact_type=SummaryArtifactType.TARGET_POPULATION,
                    title=None,
                    position=position,
                    content_json={"text": s},
                    source_chunk_ids=[cid],
                    source_refs=_refs_for([cid], lookup),
                )
            )
            position += 1
            if len(drafts) >= _MAX_POPULATIONS:
                return drafts
    return drafts


def _context(text: str, value: str, window: int = 40) -> str:
    idx = text.find(value)
    if idx < 0:
        return value
    start = max(0, idx - window)
    end = min(len(text), idx + len(value) + window)
    return text[start:end].strip()
