# OCR Windows 패키징

`scripts/fetch-ocr-resources.py` (windows-latest CI 전용):

1. UB-Mannheim Tesseract 설치 프로그램(버전 고정) 다운로드 + sha256 검증
2. 무설치 추출(/S /D=) 후 tesseract.exe + DLL을 `apps/desktop/src-tauri/resources/ocr/`로 복사
3. tessdata_fast 고정 커밋에서 kor/eng/osd.traineddata 다운로드 + checksum manifest 기록
4. 필수 파일 누락 시 빌드 실패

Tauri: `bundle.resources: ["resources/ocr"]` → 설치 후 앱 리소스 디렉터리에 포함,
셸이 `MEDBRIDGE_OCR_DIR`로 sidecar에 경로를 주입한다 (PATH·네트워크 비의존).

- 최초 CI 실행 시 로그의 실측 sha256을 스크립트 상수로 역기입해 고정한다 (재현 가능 빌드)
- CI가 OCR 스모크 테스트(번들 리소스로 한국어·영어 인식)를 실행한다
- 패키지 크기 영향은 CI 로그의 "총 N MB"와 릴리스 산출물에서 기록·보고한다
