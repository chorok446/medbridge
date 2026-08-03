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
# 후속 질문 한 개의 길이 상한. 이 값은 저장 후 버튼으로 그대로 재전송되므로 질문 상한
# (MAX_QUESTION_CHARS)보다 훨씬 짧아야 한다 — 칩이 패널 폭을 밀어내지 않을 정도로 짧게.
FOLLOWUP_MAX_CHARS = 120

# 비의료용 안전 고지 (프런트에서도 항상 표시)
MEDICAL_DISCLAIMER = "이 답변은 학습 보조용이며 실제 진단·처방·응급 판단에 사용하지 마세요."

VALID_ANSWER_STATUSES = (
    "answered",
    "not_found",
    "insufficient_evidence",
    "conflicting_evidence",
)

# 스트리밍(4B) 네트워크 제한
STREAM_CONNECT_TIMEOUT_SEC = 15.0
STREAM_IDLE_TIMEOUT_SEC = 30.0  # 청크 사이 무응답 상한
STREAM_TOTAL_DEADLINE_SEC = 180.0  # 전체 스트림 deadline
STREAM_MAX_LINE_BYTES = 64 * 1024
STREAM_MAX_TOTAL_BYTES = 8 * 1024 * 1024

# draft 체크포인트 — 이벤트마다 commit하지 않고 묶어 저장
DRAFT_CHECKPOINT_MS = 500
DRAFT_CHECKPOINT_CHARS = 1024

# 스트림 중 revision·동의 재확인 주기(검증한 claim 개수 기준)
REVISION_RECHECK_EVERY_CLAIMS = 3

# 공급자 이벤트 대기 폴링 간격 — 취소·연결 끊김을 이 주기로 감지한다(heartbeat와 별개).
STREAM_POLL_INTERVAL_SEC = 0.5
