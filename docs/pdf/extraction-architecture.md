# PDF 추출 아키텍처 (Sprint 2)

엔진: **PyMuPDF 1.26.x 고정** (`pymupdf==1.26.*`, lock 파일 관리). AI·외부 서비스 없음.

## 파이프라인 (`app/services/extraction/`)

```text
검증 완료(ready) → 자동 시작 (수동: POST /extract, 재처리: /extract/retry)
페이지 열기 → 크기·회전 → 블록(dict)·줄·span → 단어(words) → 이미지 영역
→ 표 후보(find_tables) → 텍스트 밀도 → 스캔 판정 → 읽기 순서 → 머리말·꼬리말(문서 전체 2-pass)
→ 페이지 단위 저장(교체 방식) → 문서 상태 집계
```

- 페이지 실패는 격리된다: 실패 페이지는 FAILED 행으로 남고 나머지는 계속 처리
- 문서 상태: `extracting → extracted | partially_extracted | ocr_required | extraction_failed`
- 취소: 상태를 ready로 되돌리면 파이프라인이 페이지 경계에서 감지·중단
- 재시작 복구: extracting에 멈춘 문서는 기동 시 재실행 (페이지 교체 저장이라 안전)
- 캐시(§18): documents에 engine/engine_version/schema_version/completed_at 저장 —
  동일하면 자동 재처리 안 함, 사용자는 retry로 강제 재처리 가능

## 저장 모델

document_pages(1행/페이지) ← document_blocks ← document_lines, document_tables.
전부 SQLite 호환 타입, bbox는 원본 페이지 좌표(pt).
원본 raw_text와 정규화 normalized_text 분리 보존.
단어 단위 테이블(document_words)은 0013에서 제거했다 — 읽는 곳이 페이지당 COUNT(*)
하나뿐이어서 그 값만 document_pages.digital_word_count로 남겼다. 단어 좌표가 필요한
기능을 만들려면 재추출해야 한다.

## 로그 정책 (§19)

기록: document UUID·page number·처리 시간·문자/블록 수·상태·오류 코드·엔진 버전.
금지: PDF 전체 텍스트·추출 문단·파일명·문서 내부 수치/이름.

## 임계값

모든 판정 임계값은 `thresholds.py` 한 곳에 있으며 단위 테스트로 고정된다.
실측 fixture 기준으로 조정 가능한 캘리브레이션 노브다.
