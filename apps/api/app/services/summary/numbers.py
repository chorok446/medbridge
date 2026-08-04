"""중요 수치·대상 집단의 결정론적 후보 추출.

모델이 아니라 정규식으로 후보를 먼저 수집한다. 최종 수치 문자열이 실제 청크 원문에
존재하는지 반드시 검증한다(모델이 없는 수치를 만들 여지를 두지 않는다). 새로운 임상
판단·진단은 생성하지 않는다 — 원문에 있는 수치를 그대로 인용할 뿐이다.

**문서 전체를 훑은 뒤 고른다.** 문서 순서로 훑다가 상한에 닿는 즉시 멈추면 목록이
앞부분에 갇힌다 — 실기기에서 1,060페이지 교재의 수치 30개·대상 집단 15개가 전부
2~19페이지(판권지·머리말·목차)에서 나왔고, 본문 1,041페이지는 한 항목도 기여하지
못했다. 상한은 "어디까지 읽을지"가 아니라 "무엇을 남길지"의 문제다.
"""

import re
from dataclasses import dataclass

from app.models.enums import SummaryArtifactType
from app.services.summary.schema import ArtifactDraft, ChunkRef, _refs_for

# 숫자+단위/백분율/연령/기간/용량/기준범위/연구대상수 후보. 한국어·영어 단위 혼용.
_UNITS = r"mg|g|kg|mcg|µg|ug|ml|mL|L|mmHg|bpm|mmol/L|mg/dL|IU|단위"
# 천단위 쉼표를 숫자의 일부로 읽는다. 없으면 "1,000 ml"이 "000 ml"로 잘려 저장된다.
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# 숫자와 단위 사이는 **같은 줄의** 공백만 허용한다. `\s`는 개행까지 먹어서 OCR 문서에서
# 줄 끝 숫자와 다음 줄 첫 단어가 "3.0\nmg" 한 덩어리로 저장됐다.
_GAP = r"[ \t]?"
_NUMBER_PATTERNS = [
    rf"(?:{_NUM}){_GAP}%",  # 백분율
    rf"(?:{_NUM}){_GAP}(?:{_UNITS})",  # 용량·수치+단위
    rf"\d+{_GAP}(?:세|살)",  # 연령
    rf"(?:{_NUM}){_GAP}(?:년|개월|주|일|시간|분)",  # 기간
    rf"(?:{_NUM}){_GAP}[-~]{_GAP}(?:{_NUM})",  # 범위 (10-20)
    rf"[Nn]{_GAP}={_GAP}\d+",  # 연구 대상 수
]
_NUMBER_RE = re.compile("|".join(f"(?:{p})" for p in _NUMBER_PATTERNS))

# 판권지의 발행·개정 연도. "1993년"은 임상 수치가 아닌데 기간 패턴에 걸린다.
# 짧은 기간("5년 생존율")은 임상적으로 의미가 있으므로 4자리 연도만 뺀다.
_PUBLICATION_YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s?년$")

_UNIT_VALUE_RE = re.compile(rf"\d{_GAP}(?:{_UNITS})$")
_RATIO_OR_AGE_RE = re.compile(r"(?:%|세|살)$|^[Nn]\s?=")

# 대상 집단 후보 — 포함/제외 기준, 대상 표현이 있는 문장
_POPULATION_HINTS = ("대상", "환자", "포함 기준", "제외 기준", "참여자", "군", "그룹")

# 목차 줄 — 점선 리더, 끝에 붙은 쪽번호, "Part 3" 같은 편 표시.
# 실기기에서 "쿠싱증후군 555", "Part 10 중환자 /901"이 대상 집단으로 저장됐다.
_TOC_LINE_RE = re.compile(r"\.{3,}|…|[\s/]\d{2,4}\s*$|^Part\s+\d+|^제?\s?\d+\s?장")
# 문장으로 끝나는가. 대상 집단은 서술문이다 — 명단("중환자: 이진우")이나 목차
# 표제어("급성 관동맥 증후군")는 서술이 아니다.
_SENTENCE_END_RE = re.compile(r"(?:[.!?。]|다|음|함|됨)\s*$")

_MAX_NUMBERS = 30
_MAX_POPULATIONS = 15
# 한 청크가 목록을 독점하지 못하게 한다. 표 한 장에 수치가 수십 개 몰린 페이지가
# 목록 전체를 차지하면 문서 전체를 대표하지 못한다.
_MAX_NUMBERS_PER_CHUNK = 3
_MAX_POPULATIONS_PER_CHUNK = 2


@dataclass
class _Candidate:
    order: int  # 문서 순서 — 동점 정렬과 최종 배치에 쓴다
    chunk_id: str
    text: str
    score: int


def _number_score(value: str) -> int:
    """임상적 구체성 — 상한을 넘칠 때 무엇을 남길지의 기준.

    단위가 붙은 값(250mg, 140 mmHg)이 가장 구체적이고, 비율·연령이 그 다음,
    맥락 없는 범위("1-2")가 가장 약하다.
    """
    if _UNIT_VALUE_RE.search(value):
        return 3
    if _RATIO_OR_AGE_RE.search(value):
        return 2
    return 1


def _pick(candidates: list[_Candidate], limit: int, per_chunk: int) -> list[_Candidate]:
    """문서 전체에 고르게, 구간 안에서는 구체적인 것부터 고른다.

    점수 순으로만 고르면 동점이 문서 순서로 깨져 앞쪽이 목록을 다 차지한다(실측:
    1,060페이지 교재에서 30개가 15~68페이지에 몰렸다). 문서를 limit개 구간으로 나눠
    구간마다 한 개씩 돌아가며 뽑으면, 뒤쪽 장(章)도 반드시 대표된다.
    """
    if not candidates:
        return []
    span = max(c.order for c in candidates) + 1
    buckets: list[list[_Candidate]] = [[] for _ in range(limit)]
    for cand in candidates:
        buckets[min(limit - 1, cand.order * limit // span)].append(cand)
    for bucket in buckets:
        bucket.sort(key=lambda c: (-c.score, c.order))

    per_chunk_count: dict[str, int] = {}
    chosen: list[_Candidate] = []
    while len(chosen) < limit:
        progressed = False
        for bucket in buckets:
            if len(chosen) >= limit:
                break
            while bucket:
                cand = bucket.pop(0)
                if per_chunk_count.get(cand.chunk_id, 0) >= per_chunk:
                    continue
                per_chunk_count[cand.chunk_id] = per_chunk_count.get(cand.chunk_id, 0) + 1
                chosen.append(cand)
                progressed = True
                break
        if not progressed:  # 남은 후보가 청크 상한에 다 걸렸다
            break
    return sorted(chosen, key=lambda c: c.order)


def extract_number_artifacts(
    lookup: dict[str, ChunkRef], *, start_position: int
) -> list[ArtifactDraft]:
    """청크 원문에서 수치를 추출해 IMPORTANT_NUMBER artifact를 만든다."""
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    order = 0
    for cid, ref in lookup.items():
        for match in _NUMBER_RE.finditer(ref.text):
            value = match.group(0).strip()
            # 실제 원문에 존재하는지 재확인 (검증 요구사항 — regex 매치 자체가 원문 근거)
            if value not in ref.text or value in seen:
                continue
            if _PUBLICATION_YEAR_RE.match(value):
                continue
            seen.add(value)
            candidates.append(
                _Candidate(order=order, chunk_id=cid, text=value, score=_number_score(value))
            )
            order += 1

    return [
        ArtifactDraft(
            artifact_type=SummaryArtifactType.IMPORTANT_NUMBER,
            title=cand.text,
            position=start_position + i,
            content_json={
                "value": cand.text,
                "context": _context(lookup[cand.chunk_id].text, cand.text),
            },
            source_chunk_ids=[cand.chunk_id],
            source_refs=_refs_for([cand.chunk_id], lookup),
        )
        for i, cand in enumerate(
            _pick(candidates, _MAX_NUMBERS, _MAX_NUMBERS_PER_CHUNK)
        )
    ]


def _looks_like_population_sentence(s: str) -> bool:
    """대상 집단을 서술한 문장인가.

    목차 표제어·집필진 명단·머리말 조각이 힌트 단어 하나로 통과하던 자리다.
    """
    if len(s) < 12 or len(s) > 300:
        return False
    if _TOC_LINE_RE.search(s):
        return False
    if not _SENTENCE_END_RE.search(s):
        return False
    return any(h in s for h in _POPULATION_HINTS)


def extract_population_artifacts(
    lookup: dict[str, ChunkRef], *, start_position: int
) -> list[ArtifactDraft]:
    """대상 집단을 서술한 문장을 TARGET_POPULATION 후보로 만든다."""
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    order = 0
    for cid, ref in lookup.items():
        for sentence in re.split(r"(?<=[.!?。\n])\s+|\n", ref.text):
            s = sentence.strip()
            if s in seen or not _looks_like_population_sentence(s):
                continue
            seen.add(s)
            candidates.append(_Candidate(order=order, chunk_id=cid, text=s, score=1))
            order += 1

    return [
        ArtifactDraft(
            artifact_type=SummaryArtifactType.TARGET_POPULATION,
            title=None,
            position=start_position + i,
            content_json={"text": cand.text},
            source_chunk_ids=[cand.chunk_id],
            source_refs=_refs_for([cand.chunk_id], lookup),
        )
        for i, cand in enumerate(
            _pick(candidates, _MAX_POPULATIONS, _MAX_POPULATIONS_PER_CHUNK)
        )
    ]


def _context(text: str, value: str, window: int = 40) -> str:
    idx = text.find(value)
    if idx < 0:
        return value
    start = max(0, idx - window)
    end = min(len(text), idx + len(value) + window)
    return text[start:end].strip()
