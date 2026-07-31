# MedBridge Worker

worker는 별도 코드베이스가 아니라 `apps/api`의 `app.workers.tasks` 모듈을 Dramatiq CLI로 실행하는 프로세스다.
모델·서비스 코드를 API와 공유하며 사본을 만들지 않는다 (구현 계획 §10-4).

실행 (Docker Compose가 자동으로 수행):

```bash
cd apps/api
uv run dramatiq app.workers.tasks --processes 1 --threads 4
```

- 브로커: Redis (`REDIS_URL`)
- 등록된 작업: `validate_file` — 업로드된 PDF의 실제 검증 (시그니처·암호화·손상·페이지 수·해시 일치)
- 작업 원칙: idempotent, 최대 3회 시도, 지수 백오프, correlation id 유지
