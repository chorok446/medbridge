"""명시적인 정의 요청에 질문의 제목만 반복한 비답변을 거부한다.

일반 의미 정확성 판별기가 아니다. 질문에 이미 나온 짧은 출처 제목과 같은 단어만
되풀이했는지 확인한다. 제목 조회·약어 확장·실제 설명 문장에는 적용하지 않는다.
"""

import re
import unicodedata

from app.services.qa.context import QaChunkRef

_DEFINITION = re.compile(
    r"(?:정의|의미|뜻)(?:[를을는은이가의란]|\s|[?？.]|$)|\b(?:definition|meaning)\b"
)
_TITLE_QUERY = re.compile(r"(?:목차|제목|표제)\s*(?:만|은|이|을)|\b(?:heading|title)\b")
_LABEL = re.compile(r"(?<!\w)(?:출처|원문|인용|source|original|quote)\s*:")
_ORDINAL = re.compile(r"(?<!\w)\d+[.)]\s*")
_CITATION = re.compile(r"\[c\d{1,2}\]")


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _LABEL.sub("", _CITATION.sub("", text))
    return re.findall(r"[a-z0-9가-힣]+", _ORDINAL.sub("", text))


def is_definition_heading_only(
    question: str, text: str, ids: list[str], lookup: dict[str, QaChunkRef]
) -> bool:
    question = unicodedata.normalize("NFKC", question).casefold()
    if not _DEFINITION.search(question) or _TITLE_QUERY.search(question):
        return False
    words = set(_tokens(text))
    question_key = "".join(_tokens(question))
    if not words:
        return False
    for cid in ids:
        ref = lookup[cid]
        # section_title이 없는 목차 전용 청크도 다룬다. 긴 본문을 제목으로 추측하지 않는다.
        for candidate in (ref.section_title, ref.text):
            if not candidate or len(candidate) > 120:
                continue
            title_words = _tokens(candidate)
            title_key = "".join(title_words)
            if title_key and title_key in question_key and words.issubset(title_words):
                return True
    return False
