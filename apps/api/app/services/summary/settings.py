"""요약 파이프라인 상수."""

PROMPT_VERSION = "3b-1"
SCHEMA_VERSION = 1

# 그룹 하나에 넣는 청크 텍스트 최대 문자 수 (모델 입력 길이 제한). 초과 시 섹션을 분할한다.
GROUP_MAX_CHARS = 6000
# map 요약 최대 길이
GROUP_SUMMARY_MAX_CHARS = 1200

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
