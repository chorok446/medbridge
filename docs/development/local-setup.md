# 개발 환경 (개발자 전용)

요구 도구: uv(Python 3.11+), Node 22 + pnpm, Rust stable(Tauri용). Docker 불필요.

```bash
make setup          # .env 생성 + 전체 의존성 설치
make dev-desktop    # Tauri 개발 앱 (sidecar 자동 기동, 권장)
```

브라우저로 개발할 때:

```bash
make dev-api        # sidecar: http://127.0.0.1:8765 (자동 마이그레이션)
make dev-web        # GUI: http://localhost:3000 (CORS 허용됨)
```

검증:

```bash
make test           # 백엔드 80+ / 프론트 21+ — 외부 인프라 없이 전부 로컬
make lint typecheck
make sample         # 실제 HTTP로 업로드→검증→미리보기→삭제 흐름 확인
```

## 개발 데이터

- 개발 실행 시 앱 데이터: OS 앱 데이터 경로 (또는 `MEDBRIDGE_APP_DATA_DIR`로 지정)
- 테스트는 임시 디렉터리에 격리되며 저장소 파일을 건드리지 않는다 (테스트로 강제)
- `.env`는 git에 추적되지 않는다 — `.env.example`만 커밋

## 유의

- 상태 전이는 반드시 `state_machine.transition()` 경유
- 스키마 변경은 Alembic 리비전으로만 (upgrade/downgrade 필수)
- 사용자 노출 문구에 기술 용어 금지 — `docs/user/` 문서와 `lib/format.ts` 참조
- 커밋·PR에 AI 서명 트레일러를 넣지 않는다
