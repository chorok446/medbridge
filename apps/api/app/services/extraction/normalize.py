"""텍스트 정규화 — 원문(raw)은 보존하고 정규화본을 따로 만든다.

수치·단위·소수점·괄호·±·비교연산자·위첨자 의미를 바꾸지 않는다.
의학 약어를 확장하지 않는다.
"""

import re
import unicodedata

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE = re.compile(r"[ \t ]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
# 줄 끝 하이픈 + 다음 줄 소문자 시작 → 단어 연결 후보 (라틴 소문자에 한정해 보수적으로)
_HYPHEN_JOIN = re.compile(r"([a-z])-\n([a-z])")
_PAGE_NUMBER_LINE = re.compile(r"^\s*(?:-\s*)?\d{1,4}(?:\s*-)?\s*$")


def normalize_text(raw: str) -> str:
    text = unicodedata.normalize("NFC", raw)
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_JOIN.sub(r"\1\2", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def is_page_number_line(line: str) -> bool:
    return bool(_PAGE_NUMBER_LINE.match(line))


def valid_char_ratio(text: str) -> float:
    """출력 가능한(공백 제외) 문자 비율 — 폰트 매핑이 깨진 추출을 걸러낸다."""
    stripped = [c for c in text if not c.isspace()]
    if not stripped:
        return 1.0
    good = sum(1 for c in stripped if c.isprintable() and c != "�")
    return good / len(stripped)


def page_normalized_text(block_texts: list[str], *, exclude_flags: list[bool]) -> str:
    """읽기 순서로 정렬된 블록 텍스트에서 머리말·꼬리말·표 중복을 제외한 본문 생성.

    exclude_flags[i]가 True인 블록은 제외한다 (원문 블록 자체는 DB에 그대로 남는다).
    """
    parts = [
        normalize_text(t)
        for t, excluded in zip(block_texts, exclude_flags, strict=True)
        if not excluded and t.strip()
    ]
    return "\n\n".join(p for p in parts if p)
