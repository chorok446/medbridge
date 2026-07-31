# MedBridge Study

PDF 기반 개인 의학 학습 데스크톱 앱. 실사용자는 개발 지식이 없는 간호사 1인이며,
Windows 10·11 x64용 Tauri 앱으로 배포된다. **학습 보조 도구이며 실제 진단·처방·응급 판단에
사용하지 않는다.**

- 사용자 안내: [docs/user/getting-started.md](docs/user/getting-started.md)
- 아키텍처: [docs/architecture/system-overview.md](docs/architecture/system-overview.md)
- 개발 환경: [docs/development/local-setup.md](docs/development/local-setup.md)
- 제품 명세: [docs/product/medbridge-study-development-spec.md](docs/product/medbridge-study-development-spec.md)

## 구조

```text
apps/
  api/       FastAPI sidecar (SQLite + 로컬 파일 저장 + 인프로세스 작업 실행)
  web/       Next.js 정적 GUI (Tauri에 포함)
  desktop/   Tauri 2 셸 (창·sidecar 기동·업데이트)
docs/        제품·아키텍처·사용자·보안 문서
scripts/     개발 검증 스크립트
```

## 개발 명령 (개발자 전용 — 실사용자는 설치 파일만 사용)

| 명령 | 설명 |
|---|---|
| `make setup` | 의존성 설치 + `.env` 생성 |
| `make dev-desktop` | Tauri 개발 앱 실행 (sidecar 자동 기동) |
| `make dev-api` / `make dev-web` | sidecar·GUI 개별 실행 (브라우저 개발) |
| `make test` | 전체 테스트 — PostgreSQL/Redis/MinIO/Docker 불필요 |
| `make lint` / `make typecheck` | 린트·타입 검사 |
| `make migrate` | 마이그레이션 수동 적용 (앱 시작 시 자동 실행됨) |
| `make sample` | 샘플 PDF 업로드 전체 흐름 검증 (`make dev-api` 필요) |

## 브랜치 정책

```text
develop: 개발 및 통합
main:    실사용자 안정 배포 (병합 시 Windows 릴리스 자동 생성)
feature/*: 개별 기능
hotfix/*:  main 긴급 수정
```

## 데이터 위치

사용자 데이터는 저장소·설치 경로와 분리된 OS 앱 데이터 디렉터리에 저장된다.
(Windows `%LOCALAPPDATA%\MedBridge`, macOS 개발 `~/Library/Application Support/MedBridge`)
업데이트·재설치는 이 디렉터리를 건드리지 않는다.
