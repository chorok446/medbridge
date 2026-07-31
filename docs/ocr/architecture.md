# OCR 아키텍처 (Sprint 2B)

엔진: **Tesseract 5** (Windows x64 실행 파일 + kor/eng/osd tessdata를 Tauri 리소스로 번들).
외부 네트워크·클라우드 OCR 없음. 실사용자는 아무것도 설치하지 않는다.

## 흐름

```text
스캔/혼합 PDF → 추출(Sprint 2)에서 requires_ocr 페이지 판정
→ 사용자가 [이미지 페이지 읽기] 클릭 (자동 실행 안 함 — §14, 설정으로 확장 가능한 구조)
→ OCR 잡(JobType.OCR_DOCUMENT, LocalTaskRunner, 페이지 순차·동시 1)
→ 페이지 렌더링(PyMuPDF, grayscale, DPI 300/200/400) → tesseract TSV
→ 단어·계층·confidence 파싱 → 픽셀→PDF 시각 좌표 변환
→ 디지털 우선 중복 제거 → OCR 행만 원자 교체 저장 (blocks/lines/words source=ocr)
→ ocr_runs 기록(엔진 버전·DPI·전처리·품질 지표) → 페이지 ocr_status
→ 문서 상태 상향(ocr_required→extracted 등) → 본문 normalized_text 갱신
```

## 바이너리 탐색

1. `MEDBRIDGE_OCR_DIR` (Tauri가 리소스 경로 주입 — PATH 비의존)
2. 개발 fallback: PATH의 tesseract

## 진행률·취소·복구

- 페이지별 `ocr_status`(pending→running→terminal)가 영속 기준 — 진행률 = 완료/전체
- 취소: OCR 잡을 CANCELLED로 바꾸면 잡 토큰 CAS로 페이지 경계에서 중단, 남은 페이지 ocr_cancelled
- 재시작: pending/running 페이지가 남아 있으면 기동 시 자동 재실행
- timeout(180s/페이지) 시 subprocess 강제 종료, 임시 렌더 이미지는 finally에서 삭제
- psm 3 결과가 비면 psm 6(단일 블록)으로 자동 재시도 (희소 페이지 보호)
