"""평가 전용 원문 구조화: 생성·필터링 없이 연속 표지와 모든 문자를 보존한다.

항목은 문자 범위이며 의미상 분류/조건 관계를 확정한 것이 아니다. 쪽수·bbox는 청크 전체
출처다. 구조가 불확실하면 단일 원문으로 되돌리고, 앱 답변/저장 경로에는 연결하지 않는다.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass

from app.qa_eval.evidence_selection import EvidenceExcerpt
from app.services.qa.context import QaChunkRef
from app.services.qa.provider import QaContextChunk
from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS, CONTEXT_MAX_CHARS, CONTEXT_MAX_CHUNKS

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
          "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX")
# 빈 줄 뒤의 문단 시작만 표지 후보로 본다. 본문 안 숫자/약어로 문장을 분할하지 않는다.
_PARAGRAPH = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*(?:\r?\n))*")
_LABEL = re.compile(r"[ \t]*([IVX]+|[Ⅰ-Ⅻ]+|[A-Z]|[0-9]{1,2})(?=[ \t\r\n.:)]|$)")


class SourceOutlineError(ValueError):
    """원문을 노출하지 않는 고정 진단 코드."""


@dataclass(frozen=True)
class LiteralUnit:
    start: int
    end: int
    text: str
    label_start: int | None = None
    label_end: int | None = None
    label_text: str | None = None


@dataclass(frozen=True)
class SourceOutline:
    evidence: EvidenceExcerpt
    layout: str  # labelled_sequence | verbatim; 의미 검증/답변 상태가 아님
    units: tuple[LiteralUnit, ...]


def _label(text: str, start: int) -> tuple[str, int, int, int] | None:
    match = _LABEL.match(text, start)
    if match is None:
        return None
    raw = match[1]
    normalized = unicodedata.normalize("NFKC", raw)
    if normalized in _ROMAN:
        family, ordinal = "roman", _ROMAN.index(normalized) + 1
    elif normalized.isdigit():
        # 24V·1.5 같은 값은 표지가 아니다. 점 뒤 숫자도 제외한다.
        tail = text[match.end():]
        if (re.match(r"\.\d", tail) or normalized.startswith("0")
                or not re.match(r"(?:[.:)]|[ \t]*(?:\r?\n|$))", tail)):
            return None
        family, ordinal = "arabic", int(normalized)
    elif len(normalized) == 1 and "A" <= normalized <= "Z":
        family, ordinal = "letter", ord(normalized) - ord("A") + 1
    else:
        return None
    return family, ordinal, match.start(1), match.end(1)


def _units(text: str) -> tuple[str, tuple[LiteralUnit, ...]]:
    starts = [0, *(match.end() for match in _PARAGRAPH.finditer(text))]
    labels = [(start, label) for start in starts if (label := _label(text, start)) is not None]
    # 두 개 이상 같은 표기 계열이 빈틈없이 증가할 때만 구조화한다.
    # 중복·역순·누락·다른 계열이 섞이면 임의로 일부를 골라 목록을 만들지 않는다.
    if (len(labels) < 2 or any(
        right[1][0] != left[1][0] or right[1][1] != left[1][1] + 1
        for left, right in zip(labels, labels[1:], strict=False)
    )):
        return "verbatim", (LiteralUnit(0, len(text), text),)
    units = []
    if labels[0][0]:
        units.append(LiteralUnit(0, labels[0][0], text[:labels[0][0]]))
    for index, (start, (_, _, label_start, label_end)) in enumerate(labels):
        end = labels[index + 1][0] if index + 1 < len(labels) else len(text)
        units.append(LiteralUnit(start, end, text[start:end], label_start, label_end,
                                 text[label_start:label_end]))
    return "labelled_sequence", tuple(units)


def build_source_outlines(
    chunks: list[QaContextChunk], lookup: dict[str, QaChunkRef]
) -> tuple[SourceOutline, ...]:
    """완전한 검색 입력을 손실 없이 구조화한다. 모델 호출·DB 쓰기·항목 선택 없음.

    호출자는 현재 문서 범위의 청크/출처를 제공해야 한다. 같은 텍스트라도 출처를 합치거나
    중복 제거하지 않는다. 잘린 입력/크기 초과/출처 불일치는 전체 요청을 거부한다.
    """
    if (len(chunks) > CONTEXT_MAX_CHUNKS
            or sum(len(chunk.text) for chunk in chunks) > CONTEXT_MAX_CHARS):
        raise SourceOutlineError("invalid_input_scope")
    seen: set[str] = set()
    for chunk in chunks:
        ref = lookup.get(chunk.chunk_id)
        if (not chunk.chunk_id or len(chunk.chunk_id) > 128 or chunk.chunk_id in seen
                or not chunk.text.strip() or len(chunk.text) > CHUNK_TEXT_MAX_CHARS
                or ref is None or ref.chunk_id != chunk.chunk_id or ref.text != chunk.text
                or not ref.content_hash or not ref.source_refs):
            raise SourceOutlineError("invalid_input_source")
        seen.add(chunk.chunk_id)
    result = []
    for chunk in chunks:
        ref = copy.deepcopy(lookup[chunk.chunk_id])
        layout, units = _units(ref.text)
        result.append(SourceOutline(
            EvidenceExcerpt(ref.chunk_id, ref.text, ref.content_hash, ref.source_refs,
                            0, len(ref.text), False), layout, units,
        ))
    return tuple(result)
