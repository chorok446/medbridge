"""요약 파이프라인 상수."""

# 노드 재사용 키의 일부다(build_context_key). 프롬프트·스키마·출력 예산이 바뀌어 이전
# 결과를 믿을 수 없게 되면 반드시 올린다 — 올리지 않으면 _find_reusable이 예전 노드를
# 그대로 돌려줘 수정이 기존 문서에 닿지 않는다.
#
# 3b-2: 프롬프트가 조용히 잘린 채 저장된 노드를 무효화한다. num_ctx 지정·절단 검출·
# 출력 예산 재시도가 들어오기 전에 만들어진 요약은 문서 앞부분이 빠져 있을 수 있고,
# 그 노드가 재사용되면 사용자는 여전히 틀린 요약을 '정상 완료'로 받는다.
PROMPT_VERSION = "3b-2"
SCHEMA_VERSION = 1

# 그룹 하나에 넣는 청크 텍스트 최대 문자 수. 모델의 **컨텍스트 창**이 실제 상한이다.
#
# 실측(qwen3:8b, 한국어): 프롬프트 토큰 ≈ 문자수 x 0.79. 6,000자 ≈ 4,700토큰 + 출력 512
# ≈ 5,200토큰이므로 native 경로가 지정하는 num_ctx=8192 안에 들어간다.
#
# Ollama가 요청한 num_ctx를 감당하지 못해 더 작게 잡는 기기가 있으므로, 초과하면
# executor가 그룹을 나눠 적응한다(_summarize_adaptively). 상한을 낮춰 추측하는 대신
# 호출 수를 줄이고 실패 시 적응하는 쪽을 택한다.
GROUP_MAX_CHARS = 6000
# 섹션 제목이 바뀌어도 그룹이 이 길이에 못 미치면 계속 채운다. 제목이 잦게 바뀌는 문서에서
# 제목마다 그룹을 끊으면 호출 수가 폭증한다(실측 4.27M자 → 3,074그룹).
#
# 상한 대비 약 83%로 잡는다. 더 낮추면 그룹이 절반만 찬 채 제목 경계에서 끊겨 호출 수가
# 다시 늘어난다. 섹션 경계는 그룹이 거의 다 찼을 때만 쓰는 정렬 장치다.
GROUP_MIN_CHARS = 5000

# 계층 reduce 한 단계에서 합치는 하위 요약 개수. 어느 단계도 모델 context를 넘지 않도록
# 고정 상한을 둔다(711 → 89 → 12 → 2 → 구조화 reduce).
REDUCE_FAN_IN = 8


def summary_node_concurrency() -> int:
    """같은 레벨에서 동시에 실행할 노드 수 (env `SUMMARY_NODE_CONCURRENCY`).

    상수가 아니라 함수인 이유: 값이 사용자 기기 설정에서 오므로 import 시점에 굳으면
    안 된다. 튜닝 근거는 `app.core.config.Settings.summary_node_concurrency` 참고.
    """
    from app.core.config import get_settings

    return get_settings().summary_node_concurrency
# map은 계층 reduce 입력용 짧은 JSON만 만든다. 로컬 모델이 불필요하게 긴 출력을
# 만들어 finish_reason=length와 잘린 JSON을 반환하지 않도록 문자·토큰을 함께 제한한다.
GROUP_SUMMARY_MAX_CHARS = 400
SUMMARY_MAP_MAX_TOKENS = 512

# 출력 상한에 걸려 JSON이 잘리면(finish_length) 응답 자체가 파싱 불가라 복구할 수 없다.
# 실기기 로그: 908노드 중 225번째가 3회 연속 finish_length로 실패해, 이미 성공한 675개
# 노드가 있는데도 매번 문서 전체가 버려졌다(temperature 0이라 재시도해도 같은 응답).
#
# 한 번은 더 넉넉한 예산으로 다시 부른다. 목적은 "잘린 JSON"을 "400자 초과"라는 복구
# 가능한 계약 위반으로 바꾸는 것이다 — 그 다음은 executor가 입력을 나눠 적응한다.
# 6,000자 그룹(≈4,740토큰) + 1,024 출력도 num_ctx=8192 안에 들어간다.
SUMMARY_MAP_RETRY_MAX_TOKENS = 1024

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
#
# 이 단일 timeout이 로컬 모델의 콜드 로드(디스크→메모리 적재)와 노드 생성 전체를 함께
# 덮는다. 카탈로그 최대 모델(19GiB)은 HDD에서 적재만 2분을 넘을 수 있으므로, 60초로
# 두면 "첫 요약은 오래 걸릴 수 있다"는 안내와 달리 앱이 먼저 요청을 끊는다.
SUMMARY_REQUEST_TIMEOUT_SEC = 300.0  # 요약 생성 요청 전체 timeout
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
# 8192로 잡는 이유: map은 그룹 6,000자(GROUP_MAX_CHARS) ≈ 4,740토큰 + 출력 512 ≈ 5,250,
# reduce는 프롬프트 약 2,000토큰 + 출력 4,096 ≈ 6,100이 필요하다. 둘 다 여유 있게 덮는
# 가장 작은 2의 거듭제곱이다(두 상한은 test_summary_provider_contract가 감시한다).
LOCAL_NUM_CTX = 8192
# 대형 문서는 모델 호출이 1,000회를 넘는다. 사이마다 언로드되면 재로딩만으로 시간이 배가된다.
LOCAL_KEEP_ALIVE = "30m"
