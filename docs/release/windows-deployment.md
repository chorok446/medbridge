# Windows 배포 (Sprint 1 기반 준비)

배포 대상: Windows 10·11 x64 전용. macOS/Linux 빌드는 만들지 않는다.

## 산출물 (main 병합 시 release.yml이 생성)

- NSIS `setup.exe` (currentUser 설치 — 관리자 권한 불필요)
- Tauri updater 패키지 + `.sig` 서명 파일
- `latest.json` (updater가 참조)
- sidecar 실행 파일 `medbridge-sidecar-x86_64-pc-windows-msvc.exe` (PyInstaller, 앱에 번들)

공개 저장소 `chorok446/medbridge-releases`에만 게시한다. 소스·환경변수·사용자 데이터는
게시하지 않는다. 앱에 GitHub 토큰을 포함하지 않는다.

## 필요한 GitHub Secrets (소스 저장소)

| Secret | 용도 |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | updater 패키지 서명 개인키 (`pnpm tauri signer generate`로 생성) |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | 위 키 암호 |
| `TAURI_UPDATER_PUBKEY` | 서명 공개키 (빌드 시 tauri.conf에 주입) |
| `RELEASES_REPO_TOKEN` | medbridge-releases 저장소에 Release를 만들 수 있는 PAT |

## 버전 정책

- `apps/desktop/src-tauri/tauri.conf.json`의 `version`이 릴리스 버전
- 같은 버전의 Release가 이미 있으면 워크플로가 실패한다 (덮어쓰기 금지) — 버전을 올려 재병합

## 코드 서명 상태

- **updater 서명**: 필수, 위 Secrets로 구현됨
- **Windows Authenticode**: 미적용. 인증서 확보 전까지:
  - 설치 시 SmartScreen 경고가 발생할 수 있음 (사용자 문서에 안내됨)
  - unsigned installer는 초기 내부 테스트에서만 사용
  - 장기 실사용 배포 전에 코드 서명 도입 권장 (인증서·원격 서명 자격증명도 Secrets로만 관리)

## Windows 실기기에서 검증할 항목 (개발 Mac에서는 검증 불가)

sidecar exe 기동 / NSIS 설치·시작 메뉴 바로가기 / 앱 재실행 후 SQLite·PDF 유지 /
업데이트 확인·서명 검증·passive 설치 / 업데이트 후 자동 마이그레이션 /
제거 후 `%LOCALAPPDATA%\MedBridge` 보존 / WebView2 부트스트래퍼 설치 흐름 /
내부 오류 비노출. → Sprint 1.5에서 수행.

## Sprint 1.5 남은 작업

updater 키 발급·Secrets 등록, 업데이트 전 안전 절차 GUI(작업 확인→차단→checkpoint→백업),
백업·복원 GUI, 실패 시 복구 화면("학습자료는 삭제되지 않았습니다…"), Windows 실기기 QA.
