"""Q&A 파이프라인 상수."""

PROMPT_VERSION = "4a-1"

# 질문 입력 제한
MAX_QUESTION_CHARS = 2000

# 검색·컨텍스트
QA_SEARCH_LIMIT = 8  # 모델에 전달할 최대 청크 수
CONTEXT_MAX_CHARS = 12000  # 청크 텍스트 합계 상한(순위대로 절단)
CHUNK_TEXT_MAX_CHARS = 3000  # 청크 하나당 상한

# 직전 대화 문맥(질문 해석용, 근거 아님)
HISTORY_MAX_TURNS = 4  # 최근 user/assistant 메시지 쌍 수
HISTORY_MAX_CHARS = 2000

# 모델 출력
MAX_CLAIMS = 20
MAX_FOLLOWUPS = 3
ANSWER_MAX_CHARS = 4000
CLAIM_TEXT_MAX_CHARS = 600

# 비의료용 안전 고지 (프런트에서도 항상 표시)
MEDICAL_DISCLAIMER = "이 답변은 학습 보조용이며 실제 진단·처방·응급 판단에 사용하지 마세요."

VALID_ANSWER_STATUSES = (
    "answered",
    "not_found",
    "insufficient_evidence",
    "conflicting_evidence",
)
