"""OCR 설정값 — DPI·timeout·품질 임계값을 한곳에서 관리한다 (사용자 화면 비노출)."""

OCR_DEFAULT_DPI = 300  # standard 품질
OCR_LOW_MEMORY_DPI = 200
OCR_HIGH_QUALITY_DPI = 400  # 재시도 고품질 모드
OCR_MAX_IMAGE_PIXELS = 40_000_000  # 초과 시 DPI 자동 하향 (메모리 보호)
OCR_TIMEOUT_SECONDS = 180  # 페이지당 하위 프로세스 제한
OCR_LOW_CONFIDENCE_WORD = 0.60  # 이 미만이면 낮은 신뢰 단어
OCR_LOW_CONFIDENCE_RATIO_WARN = 0.30  # 낮은 신뢰 단어 비율 경고 기준
OCR_EMPTY_MIN_WORDS = 3  # 이 미만이면 빈 결과로 간주
OCR_MIN_WORD_CONFIDENCE = 0.20  # 이 미만 단어는 노이즈로 제외
OCR_DEDUPE_OVERLAP_RATIO = 0.5  # 디지털 블록과 이 이상 겹치는 OCR 단어는 제외 (디지털 우선)

QUALITY_DPI = {
    "standard": OCR_DEFAULT_DPI,
    "low_memory": OCR_LOW_MEMORY_DPI,
    "high": OCR_HIGH_QUALITY_DPI,
}
