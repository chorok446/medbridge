"""검색·청킹 임계값 — 튜닝 지점을 한곳에 모은다 (thresholds.py와 동일한 관례)."""

# --- 청킹 ---
CHUNK_TARGET_CHARS = 800  # 청크가 이 길이에 도달하면 자연 경계에서 닫는다
CHUNK_MAX_CHARS = 1600  # 단일 블록이 이보다 길면 문장 경계에서 강제로 나눈다
CHUNK_MIN_CHARS = 120  # 같은 섹션의 인접 청크가 둘 다 이 미만이면 병합
SECTION_TITLE_MAX_CHARS = 80  # 이 이하 + 한 줄이면 섹션 제목 후보로 본다

# --- 하이브리드 검색 가중치 ---
KEYWORD_WEIGHT = 0.6
VECTOR_WEIGHT = 0.4
SECTION_TITLE_BM25_WEIGHT = 2.5  # bm25(표, 1.0, 이 값) — section_title 매치 가중
BODY_BM25_WEIGHT = 1.0
SAME_PAGE_BONUS = 0.05
ADJACENT_PAGE_BONUS = 0.02

# --- 검색 결과 ---
SEARCH_PREVIEW_CHARS = 200
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
