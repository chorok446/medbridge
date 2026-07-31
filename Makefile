.PHONY: setup dev-api dev-web dev-desktop test test-api test-web lint typecheck sample migrate

setup: ## 초기 설치 (.env 생성 + 의존성)
	@test -f .env || cp .env.example .env
	cd apps/api && uv sync
	cd apps/web && pnpm install
	cd apps/desktop && pnpm install

dev-api: ## sidecar 단독 실행 (개발·브라우저 테스트용, 포트 8765)
	cd apps/api && uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload

dev-web: ## GUI 개발 서버 (브라우저, sidecar 별도 실행 필요)
	cd apps/web && pnpm dev

dev-desktop: ## Tauri 개발 앱 실행 (sidecar 자동 기동)
	cd apps/desktop && pnpm tauri dev

test: test-api test-web ## 전체 테스트 (외부 인프라 불필요)

test-api:
	cd apps/api && uv run pytest

test-web:
	cd apps/web && pnpm test

lint:
	cd apps/api && uv run ruff check .
	cd apps/web && pnpm lint

typecheck:
	cd apps/api && uv run mypy app
	cd apps/web && pnpm typecheck

migrate: ## 마이그레이션 수동 적용 (앱 시작 시엔 자동 실행됨)
	cd apps/api && uv run alembic upgrade head

sample: ## 샘플 PDF 업로드 검증 (dev-api가 떠 있어야 함)
	./scripts/upload-sample.sh
