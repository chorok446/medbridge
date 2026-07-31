# OCR 언어 데이터

기본 언어: `kor+eng` (한국어 문장 속 영어 의학 용어·약어·숫자·단위 혼용 대응).

| 파일 | 출처 | 특성 |
|---|---|---|
| kor.traineddata | tesseract-ocr/tessdata_fast (커밋 고정) | 속도 우선 정수 양자화 |
| eng.traineddata | tessdata_fast (동일 커밋) | |
| osd.traineddata | tessdata_fast (동일 커밋) | 방향·스크립트 감지 |

- 버전·checksum은 `scripts/fetch-ocr-resources.py`와 리소스 `manifest.json`에 고정
- 원문 보존 원칙: OCR 결과의 수치·단위·소수점·%·±·비교연산자·대소문자를 임의 수정하지 않고,
  의학 용어·약어를 확장/교정하지 않는다 (AI 자동 교정 금지 — §9)
- 원문(raw)과 정규화(normalized)는 분리 저장 (정규화는 공백·유니코드 수준만)
- 관찰된 한계: kor+eng 조합은 순수 한국어 문서에서 라틴 후보가 경합해 kor 단독보다
  오인식이 늘 수 있음 → 향후 언어 자동 감지/설정의 고급 옵션 후보
