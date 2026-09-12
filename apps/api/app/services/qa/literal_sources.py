"""순수 원문의 누락된 출처 또는 전체 청크를 잘못 연결한 실제 ID를 복원한다.

본문을 생성·수정하거나 출처 청크의 텍스트/해시를 합쳐 덮어쓰지 않는다.
최대 네 청크, 같은 단일 페이지, 빈틈없는 읽기 순서와 일의적인 원문 일치를 요구한다.
복원한 ID도 호출자의 기존 수치·어휘·부정 검증을 모두 통과해야 한다.
잘못된 ID 교체는 짧은 전체 청크의 유일한 일치만 허용하며 부분 인용은 구제하지 않는다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.qa.settings import CHUNK_TEXT_MAX_CHARS, CLAIM_TEXT_MAX_CHARS

if TYPE_CHECKING:
    from app.services.qa.context import QaChunkRef

_MAX_SPAN_CHUNKS = 4


@dataclass
class _Fragment:
    cid: str
    text: str
    page: int
    first: int
    last: int


def _literal(text: str) -> str:
    # 전각 괄호와 줄바꿈만 정규화한다. 어휘·수치·문장 내부 공백은 제거하지 않는다.
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _fragment(cid: str, ref: QaChunkRef) -> _Fragment | None:
    refs = ref.source_refs
    if not refs or any(
        type(r.get("pageNumber")) is not int or type(r.get("readingOrder")) is not int
        for r in refs
    ):
        return None
    pages = {r["pageNumber"] for r in refs}
    orders = {r["readingOrder"] for r in refs}
    if len(pages) != 1 or len(orders) != max(orders) - min(orders) + 1:
        return None
    text = _literal(ref.text[:CHUNK_TEXT_MAX_CHARS])
    if not text:
        return None
    return _Fragment(cid, text, next(iter(pages)), min(orders), max(orders))


def _spanning_match(quote: str, span: list[_Fragment]) -> bool:
    # PDF 줄 경계에서 갈라진 한글 단어만 붙임/공백 두 경우를 검사한다.
    # 숫자나 영문 단어의 경계를 제거해 다른 값·단어로 만들지 않는다.
    variants = [(span[0].text, 0)]
    for fragment in span[1:]:
        next_variants = []
        for text, _ in variants:
            separators = [" "]
            if re.fullmatch("[가-힣]", text[-1]) and re.fullmatch("[가-힣]", fragment.text[0]):
                separators.append("")
            for separator in separators:
                next_variants.append((text + separator + fragment.text, len(text + separator)))
        variants = next_variants
    for text, last_start in variants:
        start = text.find(quote)
        while start >= 0:
            end = start + len(quote)
            if (start < len(span[0].text) and end > last_start
                    and (start == 0 or not text[start - 1].isalnum())
                    and (end == len(text) or not text[end].isalnum())):
                return True
            start = text.find(quote, start + 1)
    return False


def complete_literal_source_ids(
    text: str, ids: list[str], lookup: dict[str, QaChunkRef]
) -> list[str]:
    """기존 연속 인용은 확장하고, 유일한 전체 원문의 잘못된 실제 ID만 교체한다."""
    if not ids or len(text) > CLAIM_TEXT_MAX_CHARS or any(cid not in lookup for cid in ids):
        return ids
    quote = _literal(text)
    if len(quote) < 24:
        return ids  # 짧은 제목/용어는 문장 범위 복원의 대상이 아니다.
    fragments = [f for cid, ref in lookup.items() if (f := _fragment(cid, ref)) is not None]
    by_start: dict[tuple[int, int], list[_Fragment]] = {}
    for fragment in fragments:
        by_start.setdefault((fragment.page, fragment.first), []).append(fragment)
    # 원문 일부만 떼어 조건/예외를 버린 문장은 다른 ID로 구제하지 않는다.
    # 비교 범위에 같은 인용이 포함된 다른 청크가 있어도 출처를 추측하지 않는다.
    containing = [cid for cid, ref in lookup.items()
                  if quote in _literal(ref.text[:CHUNK_TEXT_MAX_CHARS])]
    whole = [f.cid for f in fragments
             if f.text == quote and lookup[f.cid].chunk_id == f.cid
             and len(lookup[f.cid].text) <= CLAIM_TEXT_MAX_CHARS]
    rebind = whole if len(whole) == 1 and containing == whole else []
    # 겹치는 위치는 연속 대안의 유일성을 확인할 수 없으므로 ID 교체를 보류한다.
    if any(len(group) != 1 for group in by_start.values()):
        rebind = []
    matches: set[tuple[str, ...]] = set()
    cited = set(ids)
    for first in fragments:
        if len(by_start[(first.page, first.first)]) != 1:
            continue
        span = [first]
        for _ in range(_MAX_SPAN_CHUNKS - 1):
            following = by_start.get((span[-1].page, span[-1].last + 1), [])
            if len(following) != 1:
                break
            span.append(following[0])
            span_ids = tuple(f.cid for f in span)
            if _spanning_match(quote, span):
                rebind = []  # 여러 청크에도 걸친 인용이면 전체 청크 교체는 하지 않는다.
                if cited.issubset(span_ids):
                    matches.add(span_ids)
                    if len(matches) > 1:
                        return ids
    if len(matches) == 1:
        return list(next(iter(matches)))
    return rebind if rebind and rebind[0] not in cited else ids
