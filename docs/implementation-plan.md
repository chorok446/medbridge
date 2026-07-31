# MedBridge Study 구현 계획

기준 문서: `docs/product/medbridge-study-development-spec.md` (최상위 개발 명세)
작성일: 2026-07-31
범위: Sprint 0(기반 구축) + Sprint 1(문서 업로드)만 구현 대상. 실제 진단·처방·응급 판단 기능은 어떤 단계에서도 구현하지 않는다.

---

## 1. 현재 저장소 구조와 사용 기술

### 조사 결과

- 이 저장소는 `/Volumes/extssd/Workspace/medbridge`에 **새로 생성**되었다 (2026-07-31, git init 직후).
- 워크스페이스 전체(`/Volumes/extssd/Workspace`)를 grep으로 조사한 결과 MedBridge 관련 기존 코드, 재사용 대상 코드, 충돌 가능 코드는 **존재하지 않는다**.
- 현재 파일: `docs/product/medbridge-study-development-spec.md` 1개뿐이다.

### 결론

- 명세 §21 "새 저장소에서 작업하는 것을 전제로 한다"와 정확히 일치하는 그린필드 상태다.
- 충돌 정리 대상 없음. 재사용 가능한 구조 없음. 명세 §7 저장소 구조를 그대로 채택한다.

### 채택 기술 (명세 §6.2 그대로, 임의 추가 없음)

| 영역 | 기술 |
|---|---|
| Frontend | Next.js App Router, TypeScript, TanStack Query, PDF.js, Tailwind CSS |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic |
| DB | PostgreSQL 16 + pgvector |
| Queue | Redis + Dramatiq (Redis broker — Celery 대비 설정이 적고 명세가 "Celery, Dramatiq 또는 동등한 작업 큐"로 허용) |
| Object Storage | 개발: MinIO(S3 호환), 운영: S3 호환 저장소 |
| 로컬 실행 | Docker Compose |

---

## 2. 새로 만들거나 수정할 파일 (Sprint 0–1 범위)

명세 §7 구조를 따른다. Sprint 0–1에 필요한 파일만 만들고, 이후 Sprint용 디렉터리를 미리 만들어 두지 않는다.

```text
medbridge/
  apps/
    web/                      # Next.js
      app/
        layout.tsx, page.tsx  # 대시보드(빈 상태 포함)
        documents/page.tsx            # 문서 목록·업로드
        documents/[id]/page.tsx       # 문서 상세·상태·PDF 뷰어
      components/
        document-viewer/PdfViewer.tsx # PDF.js 래퍼
        upload/UploadDropzone.tsx
        upload/ProcessingStatus.tsx
      lib/api/client.ts       # fetch 래퍼 + 오류 형식
      types/api.ts            # packages/shared-types 재수출
      package.json, tsconfig.json, tailwind.config.ts
    api/                      # FastAPI
      app/
        main.py               # 앱 팩토리, health check
        core/config.py        # Pydantic Settings 환경 변수 검증
        core/security.py      # 인증(세션/JWT), 비밀번호 해시
        core/logging.py       # 구조화 로그(개인정보 금지 필터)
        db/session.py, db/base.py
        models/user.py, models/document.py
        schemas/user.py, schemas/document.py
        api/routes/auth.py
        api/routes/documents.py       # POST/GET/DELETE /api/documents, retry
        services/documents/service.py # 업로드·해시·중복·상태 전이
        services/documents/storage.py # S3 클라이언트(원본/마스킹 경로 분리)
        services/documents/validation.py  # F-011 파일 안전 검사
        workers/tasks.py      # validate_file, extract_pdf 스텁 등록
      pyproject.toml, alembic.ini, alembic/versions/
    worker/
      Dockerfile              # api 코드베이스 공유, dramatiq 엔트리포인트
  packages/
    shared-types/             # API 응답·상태 enum의 TS 타입 (OpenAPI에서 생성)
  infra/
    docker/api.Dockerfile, web.Dockerfile
  docs/
    product/  (명세, 본 계획)
  tests/
    fixtures/  (샘플 PDF, 비PDF, 암호화 PDF)
  .env.example
  docker-compose.yml
  .github/workflows/ci.yml
  README.md
```

수정할 기존 파일: 없음 (그린필드).

---

## 3. 프론트엔드·백엔드·워커 간 데이터 흐름

```text
[web (Next.js)]
   │ 1. POST /api/documents (multipart)
   ▼
[api (FastAPI)]
   2. 안전 검사(MIME·크기·암호화) → sha256 중복 검사
   3. 원본을 UUID 키로 Object Storage(originals/)에 저장
   4. documents 행 생성(status=uploaded) → 즉시 작업 ID 응답
   5. Redis 큐에 validate_file 작업 발행 (correlation id 포함)
   ▼
[worker (Dramatiq)]
   6. validate_file → extract_pdf → (Sprint 2+: detect_privacy → …)
   7. 각 단계마다 documents.processing_status 갱신 (DB가 유일한 상태 원천)
   ▼
[web]
   8. GET /api/documents/{id} 폴링으로 상태 표시
   9. PDF 뷰어는 분석 완료 전에도 서명 URL로 원본 표시
```

- 상태의 원천은 PostgreSQL 하나다. Redis는 작업 전달용으로만 쓴다.
- web은 worker와 직접 통신하지 않는다. 모든 경로는 api를 거친다.
- 외부 AI 호출 경로는 Sprint 0–1에 존재하지 않으며, 이후 구현 시에도 worker → AI provider 인터페이스 단일 지점을 거치고 호출 전 `redacted_storage_key` 존재 + 사용자 `external_ai_allowed` 검사를 강제한다.

---

## 4. PostgreSQL, Redis, 객체 저장소 구성

`docker-compose.yml` 서비스:

| 서비스 | 이미지 | 용도 |
|---|---|---|
| postgres | `pgvector/pgvector:pg16` | 메인 DB + 벡터 검색(Sprint 3에서 사용, 확장은 Sprint 0에 활성화) |
| redis | `redis:7` | Dramatiq 브로커, rate limit |
| minio | `minio/minio` | S3 호환 저장소 |
| api / worker / web | 로컬 빌드 | 앱 |

객체 저장소 키 설계 (원본·마스킹 분리 — 명세 §16 필수 원칙):

```text
버킷 medbridge-originals   →  originals/{document_uuid}.pdf   # 원본, 접근 최소화
버킷 medbridge-redacted    →  redacted/{document_uuid}.pdf    # 마스킹본만 외부 AI로 전송 가능
```

- 두 버킷 모두 비공개. 다운로드는 만료 짧은(기본 5분) presigned URL만 사용.
- 원본 파일명은 경로에 쓰지 않고 `documents.original_filename` 컬럼에만 저장.
- 환경 변수는 `core/config.py`(Pydantic Settings)에서 기동 시 검증하고, `.env.example`에 전체 목록을 유지한다.

---

## 5. PDF 업로드와 비동기 처리 흐름

상태 머신 (명세 F-010 상태를 enum으로 고정):

```text
uploaded → validating → extracting → privacy_scan → analyzing → verifying → completed
                │            │            (Sprint 2+)   (Sprint 3+)  (Sprint 4+)
                └── failed ──┘        어떤 단계든 failed(failed_step, error_code 기록)
                                      부분 성공은 partial_completed
```

Sprint 1에서 실제 동작하는 구간은 `uploaded → validating → extracting → completed`이며, 이후 단계는 enum과 상태 전이 코드만 정의해 둔다(스키마 변경 없이 Sprint 2+에서 활성화).

처리 규칙 (명세 §10.2):

- 모든 작업은 idempotent: 작업 시작 시 현재 상태를 확인하고 이미 처리된 단계면 no-op.
- 재시도는 지수 백오프, 최대 3회. 실패 시 `failed_step`과 사용자용 원인 메시지 저장.
- 문서 삭제 시 대기 작업은 상태 검사로 자연 취소(작업 시작 시 문서 존재·상태 확인).
- 로그에는 correlation id와 document id만 남기고 파일 내용·개인정보는 남기지 않는다.
- 중복 업로드: sha256 일치 시 409가 아닌 `duplicate: true` 응답으로 기존 문서를 안내.

---

## 6. 데이터베이스 마이그레이션 순서 (Alembic)

모든 리비전에 downgrade를 제공한다 (명세 §21 Step 3).

1. `0001_extensions` — `CREATE EXTENSION IF NOT EXISTS vector, pgcrypto`
2. `0002_users` — users (email unique, study_level enum 0–3, external_ai_allowed)
3. `0003_documents` — documents + 인덱스 (user_id+created_at, sha256, processing_status)
4. `0004_document_pages` — document_pages (document_id+page_number unique)
5. `0005_document_sections_chunks` — document_sections, document_chunks (embedding vector 컬럼 포함, Sprint 3에서 채움)
6. `0006_privacy_findings` — privacy_findings (original_text_encrypted는 pgcrypto 대칭 암호화)
7. `0007_generated_claims` — generated_contents, claims, claim_sources
8. `0008_qa` — qa_threads, qa_messages
9. `0009_flashcards` — flashcards

- Sprint 0–1의 코드가 실제 사용하는 것은 1–4까지다. 5–9는 명세 §21 Step 3이 우선 구현 테이블로 지정했으므로 스키마만 먼저 확정해 이후 Sprint에서 스키마 변경 리스크를 줄인다.
- FK는 전부 `ON DELETE CASCADE`(파생 데이터) 또는 명시적 서비스 계층 삭제(저장소 객체)로 처리한다.

---

## 7. 보안 및 개인정보 위험

| 위험 | 대응 (Sprint 0–1에서 구현) |
|---|---|
| 원본 PDF에 환자 개인정보 포함 | 원본/마스킹 버킷 물리 분리. 외부 AI 전송 코드 경로 자체가 Sprint 1에 없음. 이후 전송 지점은 provider 인터페이스 1곳으로 한정하고 "redacted 존재 + 사용자 허용" 이중 가드 |
| 타 사용자 문서 접근 | 모든 문서 쿼리에 `user_id` 조건 강제 (서비스 계층 공통 함수 1곳에서 소유권 검사) |
| 악성 파일 업로드 | F-011: MIME 실검사(매직 바이트), 확장자 일치, 크기 제한, 암호화 PDF 거부, 검사 실패 시 파싱 안 함 |
| 경로 조작 | 저장 키는 서버 생성 UUID만 사용, 원본 파일명은 메타데이터로만 |
| 로그를 통한 개인정보 유출 | 로깅 필터로 파일 내용·이메일 마스킹, 개인정보 원문 로그 금지 규칙을 코드 리뷰 체크리스트에 명시 |
| 문서 URL 유출 | presigned URL 5분 만료, 버킷 비공개 |
| 비밀키 유출 | API 키·DB 암호는 환경 변수만, `.env`는 gitignore, 프롬프트·로그에 출력 금지 |
| 무차별 업로드 | 사용자별 rate limit(Redis), 파일 크기 제한 환경 변수화 |
| 삭제 요청 미이행 | DELETE API가 원본·마스킹 객체 + DB 파생 행을 함께 삭제(옵션 플래그는 명세 §9.1) |

의료 안전(명세 §15): Sprint 0–1은 AI 답변 기능이 없으므로 해당 위험 없음. 고정 고지(학습용 서비스, 응급 시 응급의료체계 이용)는 가입·업로드 화면에 Sprint 1부터 표시한다.

---

## 8. 자동화 테스트 전략

프레임워크: pytest(백엔드), Vitest + Testing Library(프론트), Playwright(E2E, Sprint 1 말).

Sprint 0–1 필수 테스트 (명세 §18.1, §21 Step 12에서 해당 범위만):

- **단위**: 파일 검증(비PDF 거부, 크기 초과, 암호화 PDF, 매직 바이트 불일치), sha256 중복 탐지, 상태 전이 유효성, 환경 변수 검증 실패 시 기동 거부
- **권한**: 타 사용자 문서 조회·삭제 403, 미인증 401
- **통합**: 업로드 → validate_file → extract_pdf → completed 전체 경로 (Docker Compose 기반, MinIO·Redis 실물 사용), 문서 삭제 시 객체·파생 행 삭제 확인
- **프론트 컴포넌트**: 업로드 드롭존의 loading/empty/error 상태, 처리 단계 표시
- **E2E (시나리오 축소판)**: 로그인 → PDF 업로드 → 상태 완료 → 뷰어에서 원문 표시 → 삭제

CI(GitHub Actions): lint(ruff, eslint) → typecheck(mypy, tsc) → 단위 테스트 → 통합 테스트(서비스 컨테이너). 테스트 삭제·skip으로 CI를 통과시키지 않는다(명세 금지 사항).

AI 평가 세트(§18.3)는 Sprint 3 이후 범위이므로 지금은 `tests/fixtures/`에 샘플 PDF만 준비한다.

---

## 9. Sprint 0과 Sprint 1의 세부 작업

### Sprint 0: 기반 구축

1. 모노레포 뼈대 생성 (§2의 구조), README, .gitignore
2. `docker-compose.yml`: postgres(pgvector)·redis·minio·api·worker·web
3. `core/config.py` 환경 변수 스키마 + `.env.example`
4. Alembic 초기화, 마이그레이션 0001–0004 작성·적용 (0005–0009는 스키마 리뷰 후 커밋)
5. 인증: 이메일+비밀번호 회원가입/로그인, 세션 쿠키(HttpOnly, SameSite), 비밀번호 argon2 해시
6. 구조화 로깅(JSON) + correlation id 미들웨어
7. `GET /healthz` (DB·Redis·스토리지 연결 확인)
8. `packages/shared-types`: OpenAPI → TS 타입 생성 스크립트
9. CI 파이프라인
10. 완료 기준: `docker compose up` 한 번으로 전 서비스 기동, 회원가입·로그인 동작, CI green

### Sprint 1: 문서 업로드

1. `POST /api/documents`: multipart 업로드, F-011 안전 검사, sha256 중복 탐지, UUID 키로 originals 버킷 저장, 즉시 작업 ID 반환
2. 상태 머신 enum + 전이 함수 (§5)
3. Dramatiq 작업 큐 연결, `validate_file`·`extract_pdf`(PyMuPDF 페이지 텍스트 → document_pages, 텍스트 추출 비율로 스캔 PDF 플래그) 구현
4. `GET /api/documents` (status·search·cursor 필터), `GET /api/documents/{id}` (단계·진행률), `POST /api/documents/{id}/retry` (실패 단계만), `DELETE /api/documents/{id}` (객체+파생 데이터 삭제 옵션)
5. web: 업로드 화면(드래그앤드롭, 진행 상태, 개인정보 업로드 경고 문구, 오류 메시지), 문서 목록, 문서 상세(처리 단계 표시)
6. PDF.js 뷰어: presigned URL로 원문 표시(분석 완료 전에도), 페이지 이동·확대
7. 대시보드 빈 상태(첫 방문 안내 3단계)
8. §8의 테스트 전부 + Playwright E2E 1본
9. 완료 기준: 명세 §19의 1번(업로드·삭제 안정 동작)과 2번의 전반부(페이지별 텍스트 저장) 충족

Sprint 1에서 하지 않는 것: OCR, 개인정보 탐지(Sprint 2), 임베딩·요약(Sprint 3), Q&A(Sprint 4), 진단·처방 기능(영구 제외).

---

## 10. 기존 코드와 충돌 없이 구현하는 방법

- 조사 결과 이 저장소에는 기존 코드가 없어 충돌 대상이 없다. 따라서 "충돌 회피"는 **앞으로의 자기 충돌 방지 규칙**으로 정의한다:
  1. 명세 §7 디렉터리 구조를 유일한 배치 기준으로 삼고, 계획에 없는 디렉터리를 임의로 추가하지 않는다.
  2. DB 스키마는 명세 §8을 그대로 따르고, 컬럼 추가·변경은 반드시 Alembic 리비전으로만 한다 (모델 파일 직접 수정 후 마이그레이션 누락 금지).
  3. 상태 enum·문서 유형 enum 등 공유 어휘는 backend 1곳(Pydantic/SQLAlchemy enum)에서 정의하고 프론트 타입은 OpenAPI 생성으로만 파생시켜 이중 정의를 막는다.
  4. worker는 api의 모델·서비스 코드를 import해서 쓰고 별도 사본을 만들지 않는다.
  5. 워크스페이스의 다른 프로젝트(`dasida`, `stem-tab` 등)는 이 저장소와 무관하며 참조·수정하지 않는다.
  6. 이후 Sprint에서 이 계획과 명세가 충돌하면 명세가 우선하고, 계획 문서를 갱신한 뒤 구현한다.

---

# 부록: 실행 구조 변경 이력

이 계획서의 1–10장은 최초 웹서비스(PostgreSQL/Redis/MinIO) 기준이며, 이후 지시로
실행 구조가 **Windows 단일 사용자 데스크톱 앱(Tauri 2 + SQLite + 로컬 파일)**으로
전환되었다. 현재 구조의 기준 문서는 `docs/architecture/system-overview.md` 와
`docs/desktop/` 디렉터리다.

## Sprint 1.5: Windows 패키징·실사용 검증·자동 업데이트 기반 (완료 범위)

구현 완료:
1. sidecar: 버전 노출(/health), runtime/sidecar.json 중복 실행 방지·stale 정리,
   파일 로그(logs/sidecar.log), prepare-update API(작업 차단→drain→WAL checkpoint→
   pre-update 백업), resume API, 오류 보고 데이터 API(파일명·토큰·원문 제외)
2. Tauri: 비동기 준비 상태(starting/ready/failed), /health 검증 후 메인 표시,
   process 플러그인(재시작), 오류 보고 zip 저장(파일 저장 대화상자),
   RunEvent::Exit 포함 sidecar 정리, 패키징 sidecar 논리 이름 해석
3. GUI: 준비 화면("MedBridge를 준비하고 있습니다"), 시작 실패 화면([다시 시작]
   [오류 정보 저장]), 업데이트 안내·진행률·실패 화면([나중에][업데이트]),
   설정의 수동 업데이트 확인·오류 정보 저장
4. CI: 검증 워크플로(버전 일치→테스트→Windows sidecar.exe 빌드+스모크→NSIS→artifact),
   릴리스 워크플로(버전 증가·중복 skip·secret 게이트→서명→latest.json→
   medbridge-releases 게시), scripts/check-versions.py
5. 테스트 122개(백엔드 98+프론트 24) — 외부 인프라 없이 전부 통과

실기기 검증 대기 (Sprint 1.5 완료 게이트 — Windows PC 필요):
- NSIS 설치·시작 메뉴·재실행 데이터 유지·제거 후 데이터 보존
- 0.1.0 → 0.1.1 실제 업데이트 시나리오 (서명 검증·마이그레이션·데이터 유지)
- 사전 준비: updater 키 생성, GitHub Secrets 4종 등록, medbridge-releases 저장소 생성
