"""요약 파이프라인 상수."""

PROMPT_VERSION = "3b-1"
SCHEMA_VERSION = 1

# 그룹 하나에 넣는 청크 텍스트 최대 문자 수. 모델의 **컨텍스트 창**이 실제 상한이다.
#
# 실측(qwen3:8b, 한국어): 프롬프트 토큰 ≈ 문자수 x 0.79. 그룹 6,000자면 약 4,700토큰이고
# 출력 512를 더하면 5,200토큰이 필요해 Ollama 기본 컨텍스트(4096)를 넘는다. 넘으면 Ollama가
# 프롬프트를 잘라 모델이 {"error": "Input is too long..."}를 돌려주고, summary 필드가 없어
# 요약 전체가 실패한다. 작은 문서는 그룹이 작아 통과하고 대형 문서만 실패했다.
#
# Ollama의 OpenAI 호환 endpoint는 요청별 num_ctx를 신뢰성 있게 받지 않으므로(실측: 2048을
# 넣어도 적용되지 않음) 서버가 컨텍스트를 강제할 수 없다. 따라서 입력을 흔한 기본값 4096에
# 맞춘다: 4,000자 ≈ 3,155토큰 + 출력 512 + 지시문 ≈ 3,750 < 4096.
GROUP_MAX_CHARS = 4000
# 섹션 제목이 바뀌어도 그룹이 이 길이에 못 미치면 계속 채운다. 제목이 잦게 바뀌는 문서에서
# 제목마다 그룹을 끊으면 호출 수가 폭증한다(실측 4.27M자 → 3,074그룹).
#
# 상한 대비 약 83%로 잡는다. 더 낮추면 그룹이 절반만 찬 채 제목 경계에서 끊겨 호출 수가
# 다시 늘어난다. 섹션 경계는 그룹이 거의 다 찼을 때만 쓰는 정렬 장치다.
GROUP_MIN_CHARS = 3300

# 계층 reduce 한 단계에서 합치는 하위 요약 개수. 어느 단계도 모델 context를 넘지 않도록
# 고정 상한을 둔다(711 → 89 → 12 → 2 → 구조화 reduce).
REDUCE_FAN_IN = 8
# map은 계층 reduce 입력용 짧은 JSON만 만든다. 로컬 모델이 불필요하게 긴 출력을
# 만들어 finish_reason=length와 잘린 JSON을 반환하지 않도록 문자·토큰을 함께 제한한다.
GROUP_SUMMARY_MAX_CHARS = 400
SUMMARY_MAP_MAX_TOKENS = 512

# 구조화 reduce는 overview·sections·keyConcepts 등을 한 번에 만들어 map보다 훨씬 길다.
# 로컬 qwen3:8b 실측(그룹 8개 x 400자)에서 completion_tokens가 1,500 안팎으로 기존 상한
# 2048의 72~74%까지 차올랐다. 여유가 얇으면 finish_reason=length로 잘린 JSON이 되어
# 요약 전체가 실패하므로 reduce에는 별도의 넉넉한 상한을 둔다.
SUMMARY_REDUCE_MAX_TOKENS = 4096

# artifact 내용 필드 최대 길이 (검증 단계에서 자른다)
OVERVIEW_MAX_CHARS = 2000
SECTION_SUMMARY_MAX_CHARS = 1500
CONCEPT_EXPLANATION_MAX_CHARS = 800
GENERIC_TEXT_MAX_CHARS = 800

DEFAULT_LEARNER_LEVEL = "nursing_student"
VALID_LEARNER_LEVELS = ("concise", "nursing_student", "experienced_nurse")

# 비의료용 안전 고지 — 모든 요약에 study_caution artifact로 항상 포함한다.
STUDY_CAUTION_NOTICE = (
    "이 내용은 학습 보조용이며 실제 환자의 진단·처방·응급 판단에 사용하지 마세요."
)

# 외부 모델 네트워크 제한 (SSRF·DoS 방어). urllib은 단일 timeout만 지원하므로
# connect/read를 분리하지 못한다 — 전체 요청 timeout으로 근사한다.
SUMMARY_REQUEST_TIMEOUT_SEC = 60.0  # 요약 생성 요청 전체 timeout
CONNECTION_TEST_TIMEOUT_SEC = 10.0  # 연결 확인은 더 짧게
# 정상/오류 응답 모두 이 크기까지만 읽는다(압축 비활성화 후 적용). 요약 JSON은 작다.
SUMMARY_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# 로컬(Ollama) 모델 요청 튜닝 — 비사고 모드·결정론적 출력 + 출력 길이 상한.
LOCAL_MAX_TOKENS = 2048
LOCAL_REASONING_EFFORT = "none"

# --- 로컬 전용 native /api/chat 경로 ---
#
# OpenAI 호환 endpoint로는 컨텍스트를 지정할 수도 조회할 수도 없어, Ollama가 기기별로 잡은
# 값에 요약 성패가 좌우됐다(실측: 같은 0.32.5에서 8192로 뜨는 기기와 더 작게 뜨는 기기).
# native 경로는 num_ctx를 직접 지정할 수 있고 JSON schema로 응답 구조를 강제할 수 있다.
#
# 8192로 잡는 이유: map은 그룹 4,000자 ≈ 3,155토큰 + 출력 512, reduce는 프롬프트 약
# 2,000토큰 + 출력 4,096이 필요하다. 둘 다 여유 있게 덮는 가장 작은 2의 거듭제곱이다.
LOCAL_NUM_CTX = 8192
# 대형 문서는 모델 호출이 1,000회를 넘는다. 사이마다 언로드되면 재로딩만으로 시간이 배가된다.
LOCAL_KEEP_ALIVE = "30m"
