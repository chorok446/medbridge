# Sprint 4C-A — 로컬 AI 모델 설치·연결 온보딩 계획

개발 지식이 없는 간호사 1인이 MedBridge 설정 화면에서 로컬 AI(Ollama)를 확인·설치·
연결하고 즉시 요약·질문을 쓸 수 있게 한다. 사용자는 터미널·주소·포트·환경변수·모델
경로를 다루지 않는다.

## 0. 현재 구조 (기존 자산 재사용)

### Summary/Q&A provider 설정
- `SummarySettings` 단일 행: `enabled`, `provider_type`(disabled|openai_compatible),
  `endpoint`, `model_name`, `is_local`. API 키는 DB에 없음.
- 팩토리: `summary/factory.py`(`get_summary_provider`, `ResolvedProviderConfig`,
  `load_settings_row`), `qa/factory.py`(`get_qa_provider`, `get_qa_streaming_provider`,
  `_resolved_config`). 셋 다 `SummarySettings` + keyring 키를 공유한다.
- Provider: `OpenAICompatibleSummaryProvider`/`OpenAICompatibleQaProvider`/
  `OpenAICompatibleStreamingQaProvider`. 요청은 `{endpoint}/chat/completions`.
  `available = endpoint && model && (is_local || api_key)` (로컬은 키 불요).
  **주의**: summary provider의 `available`은 아직 키를 필수로 요구 → 이번에 로컬 허용으로 수정.

### OS keyring
- `summary/secrets.py`: `keyring` 사용, 서비스명 `<app>.summary`, username `summary_api_key`.
  값은 응답·로그·DB에 노출 안 함(설정됨/미설정만). 테스트는 in-memory 백엔드 주입.
- 저장은 `settings_service.update_settings`가 DB commit과 keyring을 원자적으로 다룸
  (실패 시 이전 키 복원).

### 로컬 endpoint 검증 / 안전 HTTP
- `summary/endpoint.py`:
  - `validate_endpoint(raw, is_local)`: scheme/host/port/path 정규화, userinfo/query/
    fragment 거부, 로컬은 loopback 호스트만.
  - `assert_host_allowed(host, port, is_local)`: **요청 직전 DNS 해석 후 재검증**(SSRF 핵심).
    로컬은 모든 해석 IP가 loopback이어야 함.
  - `_NoRedirect`: 3xx 차단.
  - `post_json(...)`: 크기 상한·total deadline·redirect 차단·`Authorization: Bearer`.
  - `stream_lines(...)`: 취소 가능 스트리밍, idle/total deadline, UTF-8 incremental,
    줄 크기·총 크기 상한.
- 프런트 NDJSON 파서: `lib/api/qa-stream.ts`의 `createNdjsonParser`(UTF-8 경계·미완성 줄 처리).

### Envelope / 오류
- `Envelope[T]`(`schemas/common.py`), `wrap()`(`utils/responses.py`), `AppError`,
  correlation id. 스트림 시작 전 오류는 JSON AppError, 시작 후는 구조화 이벤트.

## 1. 아키텍처 결정

**프런트는 Ollama를 직접 호출하지 않는다.** 프런트 → MedBridge sidecar → Ollama.
loopback 검증·타임아웃·크기 제한·오류 은닉·테스트를 sidecar에 중앙화한다.

Ollama 주소는 **코드에 고정**: `http://127.0.0.1:11434` (사용자 입력 없음).
IP literal이라 DNS 조회 불필요. `assert_host_allowed(is_local=True)`로 재검증.

## 2. 백엔드 구성

### `app/services/local_ai/settings.py`
- `OLLAMA_BASE = "http://127.0.0.1:11434"`, `OLLAMA_OPENAI_BASE = OLLAMA_BASE + "/v1"`
- 타임아웃: status/tags 짧게(connect 2s, total 5s), test 짧게(20s), pull은 idle/total 큰 값
- 크기 상한: version/tags 응답, pull 이벤트 줄·총량
- `MIN_OLLAMA_VERSION = (0, 6, 0)` — Qwen3 지원 최소치(보수적, 문서화). 미만 → incompatible.
- **모델 allowlist + 카탈로그**: `qwen3:4b`(경량형, ~2.5GB), `qwen3:8b`(균형형, ~5.2GB,
  기본 추천), `qwen3:14b`(고품질형, ~9.3GB). 각 tier/label/approx_bytes.
- RAM 안내 임계(절대 판정 아님, 안내용): ≤8GB 4B도 경고 / 12–23GB 4B 권장·8B 선택 /
  24GB+ 8B 권장 / 32GB+ 14B 선택. GPU VRAM만으로 판단하지 않음.
- 디스크 안전 여유: `required_free = approx_bytes + 3GB` (다운로드·압축 해제 여유).
- 로컬 활성 설정값: provider_type=openai_compatible, is_local=true,
  endpoint=`http://127.0.0.1:11434/v1`, temperature=0, reasoning=none.

### `app/services/local_ai/system.py`
- `total_ram_bytes() -> int | None`: Windows는 ctypes `GlobalMemoryStatusEx`, posix는
  `sysconf`, 실패 시 None(안내 생략). `free_disk_bytes(path) -> int`: `shutil.disk_usage`.

### `app/services/local_ai/client.py` (endpoint.py 안전 HTTP 재사용, is_local=True 고정)
- `get_status()`:
  - GET `/api/version` → 파싱·검증 → `ready` / `incompatible`(버전 낮음)
  - 연결 실패/타임아웃 → `not_running` (설치 안 됨/실행 안 됨 통합 — loopback 단일 프로브로
    둘을 구분 불가. GUI가 "설치 안내 + 다시 확인"으로 양쪽 커버. 문서화된 단순화)
  - 기타 → `error`
  - (프런트 전용 `checking`은 조회 중 상태)
- `list_models()`: GET `/api/tags` → schema 검증 → allowlist 필터 →
  내부 필드(name/size/parameter_size/quantization_level/modified_at)만 추출.
- `pull_model(model)`: model allowlist 검증 후 POST `/api/pull {model, stream:true}` →
  `stream_lines` 재사용해 NDJSON 진행 dict yield. blob/digest/manifest/layer는 내부에서만.
- `test_model(model)`: POST `/v1/chat/completions` temperature=0, reasoning_effort=none,
  stream=false, 짧은 시스템·사용자 메시지(문서 원문 없음). `choices[0].message.content`
  비어있지 않은지 검증, `<think>` 제거. 실패는 안전 문구로 변환.
- GET가 필요하므로 `endpoint.py`에 `get_json(url, *, is_local, timeout, max_response_bytes)`
  추가(post_json과 동일 안전 정책, 인증 헤더 없음, redirect 차단).

### 중복 pull 차단
- `local_ai/pull_registry.py`: 모듈 전역 `_active: set[str]` + lock. pull 시작 시 등록,
  이미 진행 중이면 409. 종료 시 해제. (단일 사용자 — 동시 1개만.)

## 3. API 라우트 `app/api/routes/local_ai.py` (prefix `/api/local-ai`)
- `GET /status` → `{status, version?}` (기술 상세 최소)
- `GET /models` → 카탈로그 + 설치 여부 + 디스크 충분 여부 + RAM 안내 + 기본 추천(8B)
- `POST /models/pull` → 사전 검증(allowlist/중복/상태) 실패 시 JSON AppError,
  성공 시 `StreamingResponse`(application/x-ndjson). 진행/완료/오류 이벤트.
- `POST /test` → `{ok, message}`
- `POST /activate` → `{model, overwriteExternal?}` → SummarySettings 저장, `{settings view}`
  반환. 외부 설정 덮어쓸 때는 프런트가 확인창을 먼저 표시(overwriteExternal 없으면
  기존이 외부일 때 409 반환해 확인 유도).

Envelope·correlation id 유지. pull만 NDJSON.

## 4. provider 요청 조정 (§8)
- `is_local`일 때 OpenAI 호환 3 provider 페이로드에:
  temperature=0, `reasoning_effort="none"`, 적정 `max_tokens`, 시스템 프롬프트에 보조
  비사고 지시. 외부 provider는 **기존 요청 계약 유지**(reasoning_effort 미추가).
- 응답 content에서 `<think>...</think>` 제거(`_strip_thinking`) — thinking을 UI·history·
  로그에 저장하지 않음.
- summary provider `available`을 로컬 키 불요로 수정(qa와 일관).
- api_key 없을 때 `Authorization` 헤더를 보내지 않도록 `post_json`/`stream_lines` 조정
  (가짜 비밀을 keyring·헤더에 남기지 않음). 로컬 활성은 keyring에 아무것도 저장하지 않음.

## 5. GUI (`components/local-ai-settings.tsx`, 설정 페이지에 "로컬 AI" 섹션)
상태 머신: checking → (not_running | ready-no-model | downloading | ready | error).
- **Ollama 없음/실행 아님**: "로컬 AI 실행 프로그램이 필요합니다" + "설치 안내 열기"
  (Tauri opener로 공식 Ollama Windows 페이지) + "다시 확인". MedBridge가 설치 파일을
  내려받거나 실행하지 않음.
- **준비됨·모델 없음**: 경량/균형/고품질 설명 + 예상 저장 공간 + "내려받기". 기본 강조 8B.
  RAM 안내 표시.
- **다운로드 중**: role=progressbar 진행률, 현재 단계, 다른 설정 비활성, "앱을 종료하면
  다음 실행에서 상태를 다시 확인" 안내. 완료 후 자동 연결 확인.
- **모델 준비됨**: 선택 모델, 연결 확인, 기본 모델로 사용, 다른 모델 선택, 모델 관리 안내.
- **오류**: 실행 중인지 확인 / 저장 공간 확인 / 다시 시도 / 오류 보고서.
- 비노출: localhost, 11434, API, endpoint, bearer, OpenAI compatible, JSON, NDJSON,
  quantization, context window, stack trace.
- 접근성: progressbar role, aria-live 상태 변화, 키보드, 색상 외 상태 구분, 다운로드 중
  창 닫아도 안전.

### 프런트 파서/훅
- `createNdjsonParser`를 `lib/api/ndjson.ts`로 추출해 qa-stream과 공용화.
- `lib/api/local-ai.ts`: status/models/test/activate + `streamPull`(fetch POST +
  ReadableStream + ndjson 파서, AbortController).
- `hooks/use-model-download.ts`: 상태 머신(idle/downloading/completed/failed/cancelling),
  진행률, 중복 시작 차단, unmount 후 setState 차단(generation ref), 취소 시 연결 종료.
- `lib/tauri.ts`: `openExternalUrl(url)` — `@tauri-apps/plugin-opener`.

## 6. Tauri
- `tauri-plugin-opener`(Rust) + `@tauri-apps/plugin-opener`(npm) 추가. lib.rs 플러그인
  등록, capability에 `opener:allow-open-url`(https만). 공식 Ollama 페이지 열기용.

## 7. 실패·재시도·디스크 부족
- 상태/모델 조회 실패 → 안전 문구 + "다시 확인"(무한 polling 금지, 진입·사용자 요청 시만).
- pull 실패 → "다시 시도". 부분 파일은 MedBridge가 건드리지 않음(Ollama가 관리).
- 디스크 부족 → 다운로드 버튼 비활성 + "저장 공간 확인" 안내(필요 여유 표시).
- 연결 테스트 실패 → 메모리 부족/모델 로드 실패를 안전 문구로.

## 8. 보안 경계
- Ollama 주소 정확히 loopback 고정, 사용자 호스트 입력 불가, redirect 차단, IP literal.
- 응답 크기 제한, version/tags schema 검증, 모델 allowlist(qwen3:4b/8b/14b)만.
- 임의 registry·`insecure=true`·Ollama cloud·로그인 금지.
- 문서·질문을 pull/test에 포함하지 않음. 로그에 문서 원문·질문·모델 응답 원문 없음.
- API 키 placeholder를 비밀처럼 저장·노출하지 않음.

## 9. 데이터·삭제
- 문서 삭제 ≠ 모델 삭제. 앱 제거 시 Ollama·모델 자동 제거 안 함. `.ollama` 직접 조작 안 함.
- 모델 삭제 기능은 이번 범위 제외.

## 10. 테스트
백엔드(§13): version 정상/미실행/timeout/redirect/과대응답/잘못된 JSON, tags 정상·
allowlist 필터, pull NDJSON·UTF-8 분할·이벤트 분할·잘린 줄·중단·중복 차단, test 성공·빈
응답·모델없음, auth placeholder 비노출, 외부 주소 호출 불가, 문서 원문 미전송, activate
저장·외부 설정 보존/덮어쓰기.
프런트(§13): 상태·설치안내·다시확인·모델선택·기본8B·RAM안내·진행률·UTF-8 NDJSON·오류/
재시도·완료 후 테스트·activate·중복클릭·unmount 무시·기술정보 비노출·접근성.
기존 4A/4B/요약/검색/OCR/업로드 테스트 유지.

## 11. Windows 실기기 검증
`docs/testing/windows-local-ai-onboarding-validation.md` (§14의 18항목).

## 12. 검증·리뷰
백엔드 전체·ruff·mypy, 프런트 전체·eslint·tsc·production build, Windows 빌드 영향,
기존 회귀. 완료 후 독립 Codex 리뷰(§16 중점), 통과 시 커밋
`feat: Sprint 4C-A — 로컬 AI 모델 설치·연결 GUI`.

## 13. 범위 제외
설치파일 자동 다운로드·실행, 설치프로그램에 모델 포함, 커뮤니티 모델 검색, HF 직접
다운로드, Ollama cloud, 외부 웹검색, 모델 자동 업데이트, 다중 런타임, LM Studio,
llama.cpp 번들, fine-tuning, 의료 모델 자동 선택, 별도 프로필 영속화(단일 설정 행 유지).
