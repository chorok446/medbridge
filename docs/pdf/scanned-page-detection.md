# 스캔 페이지 판정

페이지 단위로 판정한다 — 텍스트 PDF라도 일부 페이지만 스캔일 수 있다.

## 신호

추출 문자 수 · 단어 수 · 이미지 면적 비율 · 페이지 전면 이미지 존재 ·
텍스트 블록 존재 · 유효 문자 비율(깨진 폰트 매핑 감지)

## 판정 (`scan.py`, 임계값은 `thresholds.py`)

| 조건 | verdict | requires_ocr |
|---|---|---|
| 문자 <20 + 이미지 비율 ≥0.55 또는 전면 이미지 | scanned | ✅ |
| 문자 <20 + 이미지 거의 없음 (빈 페이지) | unknown | ✕ |
| 유효 문자 비율 <0.60 (폰트 깨짐) | unknown | ✅ |
| 전면 이미지 + 문자 <120 | scanned | ✅ |
| 문자 있음 + 이미지 비율 ≥0.45 | mixed | 문자 ≤60일 때만 ✅ |
| 그 외 | digital | ✕ |

문서 집계: 전 페이지 OCR 필요 → `ocr_required`, 일부만 → `partially_extracted`.

## OCR 상태 (Sprint 2 결정)

Tesseract 번들은 Windows 실기기 검증 리스크로 **Sprint 2B로 분리** (명세 §11 fallback).
이번 빌드는 판정 + "OCR이 필요한 페이지입니다" 안내까지 제공하며,
인터페이스(`ocr.py`: OcrEngine/OcrResult/OcrWord — kor+eng·DPI 렌더·TSV 좌표→PDF 좌표
변환·단어 confidence·취소·비차단 계약)를 준비해 두었다.
