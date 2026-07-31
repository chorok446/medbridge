# OCR 구성요소 라이선스

| 구성요소 | 버전 | 출처 | 라이선스 | 배포 파일 |
|---|---|---|---|---|
| Tesseract OCR | 5.4.0 (UB-Mannheim 빌드 5.4.0.20240606) | github.com/UB-Mannheim/tesseract | Apache-2.0 | tesseract.exe + DLL |
| tessdata_fast kor | 고정 커밋 8741641 | github.com/tesseract-ocr/tessdata_fast | Apache-2.0 | kor.traineddata |
| tessdata_fast eng | 동일 커밋 | 동일 | Apache-2.0 | eng.traineddata |
| tessdata_fast osd | 동일 커밋 | 동일 | Apache-2.0 | osd.traineddata |

- 각 파일의 sha256은 번들 내 `resources/ocr/manifest.json`에 기록된다
- Apache-2.0 고지 의무: 앱 오픈소스 고지 화면(후속 구현)에 본 문서 내용을 노출할 수 있도록
  manifest + 본 문서를 단일 출처로 유지한다
- Tesseract에 포함된 제3자 라이브러리(leptonica 등)의 라이선스는 UB-Mannheim 배포본 고지를 따른다
