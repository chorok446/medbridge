# MedBridge 전체 기술 검토 및 개선 계획

> - 검토 기준일: 2026-08-13 (KST)
> - 기준 브랜치: `fix/summary-context-overflow`
> - 기준 커밋: `7d688b33e8a9`
> - 문서 상태: 검토 결과 기록. 아래 개선 사항은 아직 구현되지 않음.

## 1. 결론

현재 구현은 계층형 요약, 체크포인트 재사용, 출처 검증, Windows sidecar 수명주기,
오류 보고와 CI까지 상당히 보강되었다. 자동화 테스트도 대부분 통과한다.

다만 다음 두 판단은 분리해야 한다.

- **개발 및 추가 검증 지속:** 가능
- **300MB 이상 문서가 일반적인 Windows PC에서 안정적으로 처리된다고 보장:** 아직 어려움
- **공개 릴리스:** `HOLD` 유지 권장

가장 먼저 해결할 항목은 릴리스 승인 게이트, Q&A 안전 재시도, Rust lockfile,
대형 PDF 스트리밍 처리, 실사용 DB 마이그레이션 검증이다.

## 2. 확인된 검증 상태

### 2.1 통과

- GitHub Actions run `31253186282`: versions, backend, frontend, security,
  windows-build, summary 모두 통과
- 백엔드 테스트: `810 passed, 3 skipped`
- Ruff: 통과
- 웹 테스트: 24개 파일, `181 passed`
- ESLint: 통과
- TypeScript `tsc --noEmit`: 통과
- Next.js production build: 통과

### 2.2 통과하지 않았거나 CI가 확인하지 않는 항목

- Windows 로컬 mypy:
  `apps/api/app/services/local_ai/system.py:36-37`의 `os.sysconf` 타입 오류 2건
- `cargo check --locked`: `Cargo.toml`과 `Cargo.lock` 불일치로 실패
- `cargo test`: 로컬 GNU linker의 `export ordinal too large` 오류로 링크 실패
- CI에서 Rust 단위 테스트, clippy, 실제 설치·실행·제거 smoke를 수행하지 않음
- 보안 감사 단계가 `continue-on-error: true`이므로 취약점이 생겨도 CI가 초록일 수 있음

## 3. P0 - 릴리스 전 필수 개선

### P0-1. 릴리스 승인 게이트 우회 차단

**근거**

- `scripts/check_release_gate.py:80-87`은 `testedCommit`을 정확한 SHA가 아닌
  Git commit-ish로 처리하므로 `HEAD`도 허용한다.
- `scripts/check_release_gate.py:91-95`는 빈 `artifacts: []`를 허용한다.
- `scripts/check_release_gate.py:119-125`는 Qwen과 Windows 검증 결과의 문자열만 확인한다.
- `.github/workflows/release.yml:6`의 `workflow_dispatch`에는 ref 제한이 없다.

따라서 실제 실기기 검증 자료가 없어도 아래와 유사한 승인 데이터가 형식상 통과할 수 있다.

```json
{
  "testedCommit": "HEAD",
  "qwen3_8b": {"verdict": "default_recommended"},
  "windowsValidation": {"status": "passed"},
  "artifacts": []
}
```

**개선안**

- `testedCommit`을 40자리 소문자 hex SHA로 제한한다.
- 평가 JSON과 Windows 검증 결과를 각각 최소 1개 필수 artifact로 지정한다.
- artifact의 내부 commit, 모델 digest, installer SHA-256을 승인 파일과 교차검증한다.
- `HEAD`, 빈 artifact, 임의 텍스트 artifact, 과거 artifact 재사용을 거부하는 테스트를 추가한다.
- release job에서 `github.ref == 'refs/heads/main'`을 강제한다.
- 공개 게시 전 GitHub Environment required reviewer 승인을 적용한다.

### P0-2. 브랜치 및 공개 릴리스 보호

검토 시점의 원격 저장소에는 `main` 브랜치가 없고 `develop` 보호 규칙과 repository
ruleset도 확인되지 않았다. 현재 PR은 Draft이며 사람 리뷰 결정도 없다.

**개선안**

- `main` 브랜치를 만들고 required CI, 최신 base 반영, 최소 1명 리뷰를 강제한다.
- 공개 릴리스는 보호된 `main` 또는 서명된 `v*` 태그에서만 실행한다.
- 외부 릴리스 저장소 PAT는 보호된 Environment secret으로 이동한다.
- `contents: write` 권한은 게시 단계에만 최소 범위로 부여한다.

### P0-3. Q&A 재시도 계약 및 근거 안전성 통일

**문제 1: 화면과 서버의 재시도 가능 상태가 다름**

- `apps/web/components/document-qa.tsx:343-360`은 `interrupted` 메시지에도
  재시도 버튼을 표시한다.
- `apps/api/app/services/qa/service.py:288-315`는 `failed`와
  `revision_changed`만 허용한다.
- `apps/api/app/services/qa/stream_service.py:160-180`은 앱 재시작 시 활성 답변을
  `interrupted`로 바꾼다.

결과적으로 앱 재시작 후 보이는 재시도 버튼을 누르면 409가 발생할 수 있다.

**문제 2: 동기 재시도 경로에 인용 없는 모델 산문이 남을 수 있음**

- UI 재시도는 동기 REST 경로를 사용한다.
- `apps/api/app/services/qa/schema.py:228-315`는 claim과 citation marker를 검증하지만,
  답변 안의 인용 없는 문장 전체를 제거하지 않는다.
- `apps/api/app/services/qa/service.py:504-525`는 지원 claim이 하나라도 있으면
  `verified.answer`를 최종 본문으로 사용할 수 있다.

**개선안**

- 재시도도 일반 질문과 같은 스트리밍 안전 경로로 통일한다.
- 서버가 메시지별 `canRetry`를 반환하고 UI가 이를 사용하도록 한다.
- 최종 답변 본문을 서버가 검증된 supported claim만으로 재구성한다.
- `supported claim 1개 + 인용 없는 허위 문장`이 저장·표시되지 않는 회귀 테스트를 추가한다.
- `interrupted -> retry -> completed` 통합 테스트를 추가한다.

### P0-4. Rust lockfile과 Windows 테스트 실행

**근거**

- `apps/desktop/src-tauri/Cargo.toml:20`에는 `tauri-plugin-opener = "2"`가 있다.
- 현재 `Cargo.lock`에는 해당 의존성이 없어 `cargo check --locked`가 실패한다.
- CI는 `pnpm tauri build`만 실행하고 Rust 테스트를 실행하지 않는다.

**개선안**

- `Cargo.lock`을 갱신해 커밋한다.
- CI에 다음 검사를 추가한다.
  - `cargo fmt --check`
  - `cargo clippy --locked -- -D warnings`
  - `cargo test --locked`
  - `cargo check --locked`
- Windows Rust target은 CI와 동일한 MSVC toolchain으로 표준화한다.
- 설치 앱 실행, health 확인, 종료 후 sidecar 및 자손 프로세스 0개 확인을 자동화한다.

### P0-5. 0013 실데이터 마이그레이션 및 동일 installer 검증

**근거**

- `apps/api/alembic/versions/0013_drop_document_words.py:42-50`은 데이터를 백필한 뒤
  `document_words`를 삭제한다.
- downgrade는 삭제한 데이터를 복원하지 못한다.
- 현재 CI의 migration cycle은 주로 빈 DB upgrade/downgrade를 검증한다.
- 실제 앱 시작은 DB 백업과 VACUUM까지 포함하며 최대 600초를 기다린다.

**개선안**

- 0012 상태에 실사용 규모의 pages/words 데이터를 넣은 migration fixture를 만든다.
- 업그레이드 후 word count, 문서, 설정, 요약, Q&A 보존을 검증한다.
- `PRAGMA quick_check`와 `foreign_key_check`를 실행한다.
- 실제 사용자 DB 사본으로 백업, migration, 재기동, 복원 훈련을 수행한다.
- 백업과 VACUUM에 필요한 디스크 여유를 기동 전에 검사한다.
- 서명된 RC installer를 한 번만 만들고, 그 파일의 SHA-256을 실기기에서 검증한 뒤
  재빌드 없이 동일 파일을 공개 저장소로 승격한다.

## 4. P1 - 300MB 이상 문서 처리 안정화

### P1-1. 업로드와 PDF 검증을 파일 스트리밍 방식으로 변경

**현재 상태**

- `apps/api/app/services/documents/service.py:32-46`은 업로드 전체를 `bytearray`에 누적한다.
- `apps/api/app/services/documents/storage.py:67-68`은 저장된 원본을 `read_bytes()`로 읽는다.
- `apps/api/app/services/documents/validation.py:46-79`는 전체 bytes를 `BytesIO`로 감싼다.

300-800MB 문서는 파일 크기 외에도 Python 객체, PDF parser, OCR의 메모리가 추가된다.
저사양 PC에서는 pagefile thrashing이나 OOM 가능성이 있다.

**개선안**

- 같은 저장 디렉터리의 임시 파일로 업로드를 스트리밍한다.
- 스트리밍 중 크기 제한, `%PDF` signature, SHA-256을 증분 계산한다.
- 저장 완료 후 `fsync`와 원자적 rename을 사용한다.
- PDF parser에는 경로 또는 파일 handle을 전달한다.
- 300MB, 500MB, 800MB fixture로 peak RSS와 소요시간을 기록한다.

### P1-2. 청크 재생성의 메모리와 SQLite writer lock 축소

`apps/api/app/services/search/chunking.py:349-444`은 문서 전체의 page, block, table,
draft와 ORM row를 메모리에 구성하고, 한 트랜잭션에서 청크별 FTS insert를 수행한다.

**개선안**

- FTS insert를 `executemany` 또는 bounded batch로 변경한다.
- 새 chunk generation을 shadow 영역에 작성한다.
- 완료 시 active generation만 원자적으로 전환한다.
- 이전 generation은 별도 정리 작업으로 삭제한다.
- 계획 시점의 document revision을 저장하고 전환 직전에 다시 검사한다.

### P1-3. 요약 출처 크기를 문서 전체 크기와 분리

`apps/api/app/services/summary/executor.py:316-352`은 상위 노드의 출처를 모든 자식
chunk ID의 합집합으로 만든다. 최상위 artifact 하나에 수천 개 bbox가 붙을 수 있고,
여러 artifact에서 반복되면 DB와 API 응답이 크게 증가한다.

**개선안**

- 내부 범위인 `coverage_chunk_ids`와 사용자에게 보여줄 `evidence_chunk_ids`를 분리한다.
- reduce 단계에서 검증된 대표 근거만 제한된 수로 유지한다.
- 상세 출처는 페이지 범위, lazy loading 또는 pagination으로 제공한다.

### P1-4. 진짜 자동 재개와 일시적 오류 재시도

현재 체크포인트는 수동 재시도 시 성공 노드를 재사용하지만, 앱 재시작 후 같은 run을
자동으로 이어서 실행하지 않는다. 또한 수백 번의 모델 호출 중 timeout, 연결 오류,
429, 5xx가 한 번 발생하면 전체 run이 실패할 수 있다.

**개선안**

- source revision/hash와 provider fingerprint가 같으면 기존 run을 자동 재큐잉한다.
- timeout, connect, 429, 5xx에만 지수 backoff와 jitter를 적용한다.
- 인증, schema 오류, 취소, revision 변경은 재시도하지 않는다.
- 노드별 attempt count와 안전한 failure category를 저장한다.
- 앱 종료로 인한 중단은 사용자 실패 횟수에 포함하지 않는다.

### P1-5. 체크포인트 키와 보존 정책 보강

현재 context key는 provider/model 이름을 포함하지만 endpoint, local/native mode,
Ollama model digest까지 구분하지 않는다. 같은 이름의 다른 서버나 교체된 모델 결과를
재사용할 수 있다. 또한 재시도마다 재사용 노드와 artifact가 새로 저장되어 DB가 계속 증가한다.

**개선안**

- 비밀정보를 제외한 provider fingerprint를 추가한다.
  - 정규화 endpoint의 hash
  - local/external 및 native/compatible mode
  - model digest/version
  - 결과에 영향을 주는 generation 설정
- immutable cached node와 run별 참조를 분리하거나 `reused_from_node_id`를 둔다.
- 실패, 취소, 오래된 run과 artifact에 retention 정책을 적용한다.

### P1-6. 외부 AI 동의 철회의 즉시성 보장

외부 provider 사용 동의는 작업 시작 시 검사하지만, 장시간 요약의 각 네트워크 호출 직전에
최신 동의 및 provider 설정을 다시 확인하지 않는다. 실행 중 동의를 철회해도 남은 청크가
계속 전송될 수 있다.

**개선안**

- 각 외부 네트워크 호출 직전에 document/user 동의와 provider fingerprint를 새로 읽는다.
- 동의 철회 또는 provider 비활성화 시 활성 외부 작업을 취소한다.
- 첫 map 호출 후 동의를 철회했을 때 두 번째 요청이 전송되지 않는 테스트를 추가한다.

### P1-7. 종료, 업데이트, sidecar 장애 처리

- `apps/api/app/services/system/runtime.py:135-146`은 deadline을 계산하지만 내부
  `runner.drain()`이 장시간 block되면 timeout을 확인하지 못한다.
- `apps/desktop/src-tauri/src/lib.rs:480`의 감시 thread는 최초 readiness 확인 후 종료한다.
- 대형 작업 중 sidecar가 OOM 또는 crash로 종료되어도 앱은 계속 ready로 보일 수 있다.

**개선안**

- `asyncio.wait_for` 또는 task 상태 polling으로 quiescence 시간을 실제로 제한한다.
- 종료 시 새 작업 차단, 취소 신호, 짧은 checkpoint grace period, 강제 중단 순서로 처리한다.
- `Child::try_wait()` 기반 지속 감시와 `sidecar-failed` 이벤트를 추가한다.
- 제한된 자동 재시작과 전역 복구 화면을 구현한다.

## 5. P1/P2 - 사용자 경험 및 운영 개선

### 5.1 요약 화면

- `apps/web/components/summary-view.tsx:69-90`은 완료 상태를 polling하지만 artifact 목록을
  다시 조회하지 않아 완료 후에도 빈 화면이 남을 수 있다.
- provider가 unavailable이면 기존에 저장된 요약까지 숨겨진다.
- `cancelSummary()` API는 있지만 화면에 장시간 작업 취소 버튼이 없다.
- 상태 또는 목록 조회 실패가 빈 결과로 오인될 수 있다.

**개선안**

- active에서 terminal 상태로 전환될 때 summaries query를 invalidate/refetch한다.
- 기존 artifact 열람과 새 생성 가능 여부를 분리한다.
- 진행 중 취소 버튼과 명확한 loading/error/retry 화면을 제공한다.

### 5.2 문서 목록

- `apps/web/app/documents/page.tsx:26`은 `listDocuments()`를 한 번만 호출한다.
- 백엔드 기본 limit은 20이고 client type에는 이미 `nextCursor`가 있다.

**개선안**

- `useInfiniteQuery` 또는 `더 보기`를 구현한다.
- 업로드, 삭제, 검색 변경 시 page merge와 cursor reset을 테스트한다.

### 5.3 Q&A thread 전환

thread 목록이 아직 로딩 중일 때 질문하면 기존 thread가 있어도 새 thread가 생성될 수 있다.
스트리밍 중에도 thread 선택, 새 대화, 삭제가 가능해 표시 thread와 실행 중 stream이 어긋날 수 있다.

**개선안**

- 목록 로딩 완료 전 질문 입력과 예시 질문을 잠근다.
- 스트리밍 중 thread 전환은 취소 확인 후 수행한다.
- 목록/상세 조회 오류와 빈 thread 상태를 구분한다.

### 5.4 설정 화면

- 연결 확인이 수정 중인 draft가 아니라 저장된 설정을 검사한다.
- 저장된 API key를 UI에서 삭제할 수 없다.

**개선안**

- draft 설정을 전달해 검사하거나 저장 후 검사한다.
- 기존 delete API를 UI에 연결하고 명시적 키 삭제 동작을 제공한다.

### 5.5 로그와 오류 보고서

- `apps/api/app/core/logging.py`는 회전 없는 `FileHandler`를 사용한다.
- OCR stderr와 예외 문자열에 Windows 사용자명, temp 경로, 문서명이 포함될 수 있다.
- sidecar bootstrap stdout/stderr가 폐기되어 시작 실패 원인을 찾기 어렵다.

**개선안**

- `RotatingFileHandler`와 크기/세대 제한을 적용한다.
- home, app data, temp, resource 경로를 구조적으로 redaction한다.
- 원문 stderr 대신 단계, exit code, 분류된 오류, 최근 sanitised 로그만 보고서에 포함한다.

## 6. 구현 권장 순서

### 1단계 - 릴리스 안전성

- 릴리스 게이트 우회 차단
- `main` 및 release Environment 보호
- Cargo.lock 갱신과 Rust CI 추가
- 0013 실DB migration/복원 검증
- 동일 RC installer 승격 절차 구축

### 2단계 - 대형 문서 기반 구조

- 파일 스트리밍 업로드 및 경로 기반 PDF 검증
- 청크 generation, batch FTS, 원자적 전환
- bounded evidence citation
- DB 및 로그 retention

### 3단계 - 장시간 요약 복원력

- 동일 run 자동 재개
- 노드별 transient retry/backoff
- provider fingerprint
- 동의 철회 즉시 중단
- bounded shutdown과 sidecar 지속 감시

### 4단계 - UI 계약 및 실기기 검증

- 요약 완료 refetch 및 취소 UI
- 문서 cursor pagination
- Q&A retry/thread 계약 통일
- 300MB, 500MB, 800MB 실문서 soak test
- 업데이트, 강제 종료, 디스크 부족, 네트워크 중단 복구 시험

## 7. 출시 판단 체크리스트

아래 항목이 모두 충족되기 전까지 공개 릴리스는 `HOLD`로 본다.

- [ ] 승인 게이트가 `HEAD`, 빈 artifact, 과거 artifact 재사용을 거부한다.
- [ ] 보호된 main/tag와 Environment 승인에서만 공개 릴리스가 가능하다.
- [ ] `cargo check/test/clippy --locked`가 Windows CI에서 통과한다.
- [ ] 실제 사용자 규모 DB의 0013 migration과 backup restore가 검증됐다.
- [ ] 검증 installer와 공개 installer의 SHA-256이 동일하다.
- [ ] 300MB 이상 PDF 업로드 시 파일 전체가 Python heap에 올라가지 않는다.
- [ ] 청크 생성이 bounded batch로 동작하고 설정 저장 등 다른 SQLite write를 장시간 막지 않는다.
- [ ] 앱 재시작 후 장시간 요약이 같은 run에서 자동 재개된다.
- [ ] transient provider 오류가 전체 진행 상황을 잃게 하지 않는다.
- [ ] 외부 AI 동의 철회 후 추가 문서 전송이 발생하지 않는다.
- [ ] sidecar crash를 감지하고 사용자에게 복구 동작을 제공한다.
- [ ] Q&A 최종 본문에는 검증된 근거 문장만 표시된다.
- [ ] 300MB, 500MB, 800MB 문서의 시간, peak RAM, DB 증가량, 재시작 복구 결과가 기록됐다.

## 8. 문서 유지 규칙

- 코드 수정이 완료되면 관련 항목에 PR/commit과 검증 결과를 추가한다.
- 자동 테스트 통과와 실기기 검증 통과를 별도 상태로 기록한다.
- 동적 정보인 CI run, HEAD, installer hash, 모델 digest는 날짜와 함께 갱신한다.
- 발견 사항을 삭제하지 말고 `완료`, `수용`, `연기` 중 하나로 상태를 전환해 결정 이력을 남긴다.
