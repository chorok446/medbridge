.PHONY: setup up down clean migrate revision test test-api test-web lint typecheck upload-sample logs

setup: ## 초기 설치 (.env 생성 + 의존성)
	@test -f .env || cp .env.example .env
	cd apps/api && uv sync
	cd apps/web && pnpm install

up: ## 전체 스택 기동 + 마이그레이션
	docker compose up -d --build postgres redis minio minio-init api worker web
	docker compose run --rm migrate

down: ## 종료 (데이터 유지)
	docker compose down

clean: ## 종료 + 볼륨 초기화 (모든 로컬 데이터 삭제)
	docker compose down -v

migrate: ## 마이그레이션 적용
	docker compose run --rm migrate

revision: ## 새 마이그레이션 생성: make revision m="메시지"
	cd apps/api && uv run alembic revision -m "$(m)"

test: test-api test-web ## 전체 테스트

test-api: ## 백엔드 테스트 (통합 테스트는 스택 기동 필요)
	cd apps/api && uv run pytest

test-web: ## 프론트엔드 테스트
	cd apps/web && pnpm test

lint:
	cd apps/api && uv run ruff check .
	cd apps/web && pnpm lint

typecheck:
	cd apps/api && uv run mypy app
	cd apps/web && pnpm typecheck

upload-sample: ## 샘플 PDF 업로드 (스택 기동 + 회원가입 포함 전체 흐름)
	./scripts/upload-sample.sh

logs:
	docker compose logs -f api worker
