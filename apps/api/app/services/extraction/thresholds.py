"""추출·판정 임계값 — 튜닝 지점을 한곳에 모은다 (단위 테스트 대상).

물리 세계의 PDF는 이상적이지 않다: 스캐너·편집기별 편차가 커서
이 값들은 실측 fixture 기준으로 조정 가능한 캘리브레이션 노브다.
"""

# --- 스캔 판정 (docs/pdf/scanned-page-detection.md) ---
SCAN_MIN_CHARS = 20  # 이 미만이면 텍스트 없는 페이지로 간주
SCAN_MIN_WORDS = 3  # 이 미만이면 텍스트 없는 페이지로 간주 (OCR_EMPTY_MIN_WORDS와 동일 기준)
SCAN_DIGITAL_MIN_CHARS = 120  # 이 이상이면 텍스트 페이지로 확신
SCAN_IMAGE_RATIO_SCANNED = 0.55  # 텍스트 없음 + 이미지 비율 ≥ → scanned
SCAN_IMAGE_RATIO_MIXED = 0.45  # 텍스트 있음 + 이미지 비율 ≥ → mixed
SCAN_FULL_PAGE_IMAGE_RATIO = 0.85  # 단일 이미지가 페이지의 이 비율 이상이면 전면 스캔 신호
SCAN_MIN_TEXT_AREA_RATIO = 0.03  # 텍스트 블록 면적이 이 미만이면 "실질적 텍스트 없음" 보조 신호
SCAN_VALID_CHAR_RATIO = 0.60  # 유효(출력 가능) 문자 비율이 이 미만이면 추출 신뢰 불가
OCR_REQUIRED_MIXED_MAX_CHARS = 60  # mixed인데 문자 수가 이 이하면 OCR 권장

# --- 읽기 순서 (docs/pdf/reading-order.md) ---
FULL_WIDTH_BLOCK_RATIO = 0.72  # 본문 폭 대비 이 이상이면 전체 폭 블록(제목 등)
TWO_COLUMN_MIN_BLOCKS = 2  # 열 판정에 필요한 최소 본문 블록 수
COLUMN_STRADDLE_TOLERANCE = 0.06  # 페이지 폭 대비 중앙선 걸침 허용 비율
FOOTNOTE_ZONE_RATIO = 0.87  # 페이지 높이 대비 이 아래에서 시작하면 각주 후보
READING_ORDER_CONFIDENCE_TWO_COL = 0.85
READING_ORDER_CONFIDENCE_FALLBACK = 0.5

# --- 머리말·꼬리말 (docs/pdf/extraction-architecture.md) ---
HEADER_ZONE_RATIO = 0.10  # 페이지 상단 10%
FOOTER_ZONE_RATIO = 0.90  # 페이지 하단 10%
REPEAT_MIN_PAGES = 2  # 최소 반복 페이지 수
REPEAT_MIN_FRACTION = 0.4  # 전체 페이지 대비 반복 비율
HEADER_MAX_CHARS = 120  # 머리말·꼬리말 후보 최대 길이

# --- 캡션 ---
CAPTION_MAX_DISTANCE_PT = 60.0  # 이미지/표와의 최대 거리(pt)

# --- 표 ---
TABLE_BLOCK_OVERLAP_RATIO = 0.7  # 블록이 표와 이 비율 이상 겹치면 표 내부 텍스트로 표시

# --- 좌표 ---
BBOX_DECIMALS = 2  # 저장 소수점 자리수 (pt 단위)
