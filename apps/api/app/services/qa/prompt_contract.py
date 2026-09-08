"""Q&A 공급자 경로가 공유하는 모델 출력 계약 문구."""

CLAIM_SOURCE_AND_ABSTENTION_RULE = (
    "- 모든 claim 객체에는 text와 sourceChunkIds 필드가 반드시 있어야 한다. "
    "sourceChunkIds는 실제 근거 청크의 chunkId를 문자 하나도 바꾸지 않고 복사한 "
    "문자열 배열이다. 본문 안의 원문 인용, 제목, 쪽수는 sourceChunkIds를 대체하지 "
    "못한다. 출처 배열을 생략하거나 id를 새로 만들지 않는다. 청크 제목·쪽수·id는 "
    "인용 메타데이터이므로 claim의 text에 붙이지 않는다.\n"
    "- 질문이 요구한 사실을 청크에서 확인할 수 없으면 관련 주제의 설명으로 "
    "답을 대신하지 않는다. 이 경우 claim을 하나도 만들지 않고 not_found 또는 "
    "insufficient_evidence로 보류한다.\n"
)

CROSS_LANGUAGE_GROUNDING_RULE = (
    "- 질문에 쓰인 언어로 답한다. 질문과 근거가 같은 언어면 번역용 원문 괄호를 "
    "덧붙이지 않는다. 질문 언어가 근거 청크 언어와 다르면, 각 claim의 "
    "중복 제외 2자 이상 의미 단어 중 최소 3분의 1은 인용한 청크에 그대로 있는 "
    "원문 단어여야 한다. 번역문은 그 원문 구절을 중심으로 간결하게 쓰고 충분한 "
    "원문 단어를 함께 넣는다. 원문 단어 1개만 긴 번역문에 붙이거나 번역문만 쓰거나 "
    "원문에 없는 구절을 만들지 않는다. 예: 영어 원문이 'The library opens at 9 on "
    "weekdays'이면 '도서관은 평일 오전 9시에 엽니다 (원문: library opens at 9 on "
    "weekdays).'처럼 쓴다.\n"
)
