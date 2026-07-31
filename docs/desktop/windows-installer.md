# Windows 설치 프로그램 (NSIS)

설정 위치: `apps/desktop/src-tauri/tauri.conf.json` → `bundle`

| 항목 | 값 |
|---|---|
| 대상 | NSIS 단일 (`targets: ["nsis"]`), Windows x64 |
| 설치 범위 | currentUser — 관리자 권한 불필요 |
| 언어 | 한국어 |
| WebView2 | downloadBootstrapper (없는 PC에서 자동 설치) |
| 시작 메뉴 | Tauri NSIS 기본 생성 |
| updater | `createUpdaterArtifacts: true` → 서명(.sig) 동반 |
| 산출물 예 | `MedBridge Study_0.1.0_x64-setup.exe` |

- 제거(uninstall)는 설치 파일만 제거하며 `%LOCALAPPDATA%\MedBridge`(DB·PDF)는 남긴다.
  → Windows 실기기 검증 항목: 제거 후 데이터 잔존, 재설치 후 기존 데이터 인식.
- 코드 서명(Authenticode)은 미적용 — SmartScreen 경고 안내는
  [../release/windows-release-process.md](../release/windows-release-process.md) 참조.
- sidecar(`medbridge-sidecar-x86_64-pc-windows-msvc.exe`)는 CI가
  `src-tauri/binaries/`에 배치하며, 설치 시 메인 exe 옆에 `medbridge-sidecar.exe`로 놓인다.
