"""Q&A 전용 검색어 정리. 원래 질문과 일반 검색 API는 변경하지 않는다."""

import re

# 형태소 분석 대신 범위가 좁은 접미 조사만 처리한다. 단어 자체가 될 수 있는
# '이/가/과' 등은 자르지 않으며, 두 글자 의학 용어와 영문 약어·수치를 보존한다.
_PARTICLE = re.compile(r"^(.{2,}?)(?:에서|에게|으로|은|는|을|를|의)$")
_REQUEST_WORDS = frozenset(
    "이 그 저 한 문서 자료 원문 문장 문장과 문장으로 출처 출처와 "
    "무엇 무엇인가요 무엇인가 어떤 어떻게 설명 설명하는 설명해 설명해주세요 "
    "설명하나요 알려줘 알려주세요 보여줘 보여주세요 주세요 하나요".split()
)
# 주제가 있는 질문에서 '정의/진단 기준'만 일치하는 다른 질환이 검색되지 않게 한다.
# 이 단어들만 묻는 질문에서는 제거하지 않는다.
_ASPECT_WORDS = frozenset({"정의", "의미", "진단", "기준", "증상", "원인", "분류", "치료"})
_EDGE_PUNCTUATION = "\"'“”‘’()[]{}.,?!:;"


def keyword_query(question: str) -> str:
    terms: list[str] = []
    for raw in question.split():
        term = raw.strip(_EDGE_PUNCTUATION)
        if term in _REQUEST_WORDS:
            continue  # '설명하는'의 '-는'을 조사로 잘라 요청 표현을 되살리지 않는다.
        match = _PARTICLE.fullmatch(term)
        if match:
            term = match[1]
        if term and term not in _REQUEST_WORDS and term not in terms:
            terms.append(term)
    topics = [term for term in terms if term not in _ASPECT_WORDS]
    return " ".join(topics or terms)
