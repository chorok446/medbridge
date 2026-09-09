"""명시적 문단 범위 인용에서 모델이 생략한 원문 표기를 보수적으로 복원한다.

새 사실을 생성하지 않는다. 원문 전체 범위가 일의적이고 모델이 실제 인용한 문장과
청크가 각 문단을 모두 뒷받침할 때만 순수 인용을 교체한다. 최종 근거 검증은 호출자가
기존 경로로 수행한다. 불명확한 요청·출처·출력은 원래 모델 이벤트를 그대로 돌려준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.services.qa.settings import ANSWER_MAX_CHARS, CLAIM_TEXT_MAX_CHARS, MAX_CLAIMS

if TYPE_CHECKING:
    from app.services.qa.provider import QaRequest

_RANGE = re.compile(
    r"문단\s*([1-9][0-9]{0,2})\s*(?:부터|~|-)\s*(?:문단\s*)?([1-9][0-9]{0,2})(?!\d)"
)
_QUOTE_REQUEST = re.compile(
    r"원문(?:을|만)?\s*인용(?:해\s*(?:주세요|주십시오|줘)|하세요|하라)\s*[.!?。]?\s*$"
)
_LOCATION = re.compile(r"\d\s*(?:장|절|쪽|페이지)")
_PARAGRAPH = re.compile(r"문단\s*([1-9][0-9]{0,2})[.:：]\s+(.+)", re.DOTALL)
_NESTED_LABEL = re.compile(r"(?m)^\s*문단\s*\d+[.:：]")


@dataclass
class _Paragraph:
    text: str
    body_key: str
    source_ids: set[str] = field(default_factory=set)


def _literal_key(text: str) -> str:
    # 줄바꿈/공백과 문장 끝 마침표만 허용한다. 바꿔쓰기나 인용 앞뒤 설명은 매칭하지 않는다.
    return " ".join(text.split()).removesuffix(".")


def _source_paragraphs(request: QaRequest) -> dict[int, _Paragraph]:
    ranges = list(_RANGE.finditer(request.question))
    if len(ranges) != 1 or not _QUOTE_REQUEST.search(request.question):
        return {}
    if _LOCATION.search(request.question):
        return {}  # 장/쪽 등 추가 범위 한정은 이 좁은 복원기가 해석하지 않는다.
    start, end = map(int, ranges[0].groups())
    if not 1 <= end - start + 1 <= MAX_CLAIMS:
        return {}
    paragraphs: dict[int, _Paragraph] = {}
    for chunk in request.chunks:
        for block in re.split(r"\n\s*\n", chunk.text):
            text = block.strip()
            match = _PARAGRAPH.fullmatch(text)
            if match is None:
                continue
            number, body = int(match[1]), match[2]
            if not start <= number <= end:
                continue
            if (len(text) > CLAIM_TEXT_MAX_CHARS or _NESTED_LABEL.search(body)
                    or not body.rstrip().endswith((".", "!", "?", "。", "！", "？"))):
                return {}  # 합쳐진 문단이나 문장 끝이 없는 구절을 임의로 완성하지 않는다.
            key = _literal_key(body)
            if not key:
                return {}
            if number in paragraphs and paragraphs[number].body_key != key:
                return {}  # 같은 번호의 내용이 다르면 어느 장/쪽인지 임의로 선택하지 않는다.
            paragraph = paragraphs.setdefault(number, _Paragraph(text, key))
            paragraph.source_ids.add(chunk.chunk_id)
    if set(paragraphs) != set(range(start, end + 1)):
        return {}
    return dict(sorted(paragraphs.items()))


def restore_quote_labels(request: QaRequest, events: list[dict]) -> list[dict]:
    """완료된 로컬 모델 시도의 순수 인용만 복원한다. 보류·상충은 변경하지 않는다."""
    if any(e.get("type") == "final" and e.get("answerStatus") != "answered" for e in events):
        return events
    paragraphs = _source_paragraphs(request)
    if not paragraphs:
        return events
    evidence: dict[int, set[str]] = {number: set() for number in paragraphs}
    matched_indices: set[int] = set()
    for index, event in enumerate(events):
        text, ids = event.get("text"), event.get("sourceChunkIds")
        if event.get("type") != "claim" or not isinstance(text, str) or not isinstance(ids, list):
            continue
        cited = {cid for cid in ids if isinstance(cid, str)}
        labelled = _PARAGRAPH.fullmatch(text.strip())
        key = _literal_key(labelled[2] if labelled else text)
        for number, paragraph in paragraphs.items():
            if labelled and number != int(labelled[1]):
                continue
            valid_ids = cited & paragraph.source_ids
            if key == paragraph.body_key and valid_ids:
                evidence[number].update(valid_ids)
                matched_indices.add(index)
    if not all(evidence.values()):
        return events  # 모델이 인용하지 않은 서로 다른 문장은 새로 보충하지 않는다.
    quotes = [
        {"type": "claim", "text": p.text, "sourceChunkIds": sorted(evidence[n])}
        for n, p in paragraphs.items()
    ]
    restored: list[dict] = []
    first_match = min(matched_indices)
    for index, event in enumerate(events):
        if index == first_match:
            restored.extend(quotes)
        if index not in matched_indices:
            restored.append(event)
    claims = [e for e in restored if e.get("type") == "claim"]
    if len(claims) > MAX_CLAIMS or any(not isinstance(e.get("text"), str) for e in claims):
        return events
    # 인용 마커·개행 여유까지 포함해 기존 본문 상한을 넘도록 확장하지 않는다.
    if sum(len(e.get("text") or "") + 8 for e in claims) > ANSWER_MAX_CHARS:
        return events
    return restored
