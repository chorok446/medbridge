# Windows 데스크톱 아키텍처

배포 대상: Windows 10·11 x64 / NSIS currentUser 설치 / 사용자 데이터 `%LOCALAPPDATA%\MedBridge`.

```text
MedBridge Study.exe (Tauri 2)
├─ WebView2: Next.js 정적 GUI (out/ 포함)
├─ medbridge-sidecar.exe (같은 디렉터리에 설치되는 externalBin)
│   └─ FastAPI + SQLite + LocalFileStorage + LocalTaskRunner
└─ 플러그인: updater, process(재시작), dialog(파일 저장)
```

책임 분리:

| 영역 | Tauri | sidecar |
|---|---|---|
| 창·파일 선택·업데이트·재시작·오류 보고 저장 | ✅ | |
| SQLite 읽기/쓰기·마이그레이션·문서 처리 | | ✅ (단독 소유) |

Tauri는 SQLite를 직접 만지지 않는다 — DB 쓰기는 sidecar만 수행한다.

프론트엔드 방식: **Next.js 정적 내보내기(output: export)** 선택.
근거: Sprint 1 UI가 전부 클라이언트 컴포넌트라 서버 기능 의존이 없고, Vite 전환 대비
변경 파일이 3개(next.config, 동적 라우트→쿼리 파라미터, API base 해석)로 최소였다.

관련: [sidecar-lifecycle.md](sidecar-lifecycle.md), [local-data.md](local-data.md),
[auto-update.md](auto-update.md)
