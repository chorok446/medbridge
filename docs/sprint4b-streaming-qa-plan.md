# Sprint 4B — 검증된 출처 기반 Q&A 스트리밍 계획

Sprint 4A(출처 기반 Q&A) 위에 점진 답변 표시·취소·중단 복구를 추가한다. 핵심 안전
원칙: **미검증 의료 문장을 토큰 단위로 노출하지 않는다.** 사용자에게 보이는 점진 답변은
서버 출처 검증을 통과한 주장(claim) 단위여야 한다.

## 0. 현재 4A 구조 (기준: 4A 브랜치, migration head 0007)

- `services/qa/service.py:_generate` — 검색→모델(비스트림)→`verify`→revision-guarded 저장.
- `services/qa/schema.py:verify` — claim별 sourceChunkIds 검증(현재 문서·검색 subset),
  수치 자릿수 경계, 어휘 연결(`_lexically_grounded`), 상태 결정. **재사용**.
- `services/qa/context.py:retrieve` — 현재 문서 검색 + chunk 재조회 + hash pairs. **재사용**.
- `services/qa/provider.py` — 비스트림 `QaProvider`(유지, 4A fallback·테스트).
- `services/summary/endpoint.py` — `validate_endpoint`/`assert_host_allowed`/`post_json`
  (HTTPS/loopback·DNS 재검증·redirect 차단·크기·deadline). 스트리밍용 안전 HTTP를 여기에 추가.
- `models/qa.py` — `QaMessage`(status enum, values_callable로 소문자 저장), 부분 유니크
  `uq_qa_active_answer WHERE role='assistant' AND status='pending'`.
- revision guard: `_fresh_document`/`_fresh_user`(populate_existing), `_hash_of_ids`(검색 subset).

## 1. 마이그레이션 0008 (down_revision 0007)

`qa_messages` 컬럼 추가(전부 nullable, 기존 행 무해):
- `draft_content` Text nullable, `stream_request_id` String unique nullable,
  `stream_started_at`/`stream_updated_at`/`cancel_requested_at`/`interrupted_at` DateTime nullable,
  `last_stream_seq` Integer default 0, `retry_of_message_id` Uuid nullable(self FK).

status는 문자열이라 컬럼 변경 불필요. 추가 상태값: `streaming`, `finalizing`, `cancelled`,
`interrupted`, `consent_revoked`(enum에 추가). **활성 assistant 부분 인덱스를 재생성**해
`status IN ('pending','streaming','finalizing')`를 활성으로 포함(동시 스트림 1개 DB 보장).

## 2. NDJSON 전송 프로토콜 (`services/qa/stream_protocol.py`)

- `Content-Type: application/x-ndjson; charset=utf-8`, 한 줄 = JSON 이벤트 하나.
- 이벤트 type: `started`(requestId,messageId), `phase`(retrieving/generating/finalizing),
  `claim`(seq,claimIndex,text,sources[]), `completed`(message DTO), `cancelled`,
  `interrupted`(code,retryable), `error`(code,message,retryable), `heartbeat`(seq).
- seq 단조 증가. 줄 내부 개행은 JSON escape. 최대 이벤트/총 스트림 크기·heartbeat 주기 상한.
- 알 수 없는 type은 프런트가 무시(본문 미로깅).

## 3. 스트리밍 공급자 (`services/qa/streaming.py`)

`QaStreamingProvider` Protocol: `provider_name`/`model_name`/`available`/
`stream_answer(request, cancel_token) -> Iterator[dict]`(동기 iterator, 스레드에서 구동).
공급자 의미 이벤트: `{"type":"claim","text":...,"sourceChunkIds":[...]}` / `{"type":"final",
"answerStatus":...,"followUpSuggestions":[...]}` — **page/bbox 없음**.
- `DeterministicStreamingQaProvider`(테스트: 청크에서 claim 이벤트 생성).
- `OpenAICompatibleStreamingQaProvider`: `stream=true`, 기존 안전 HTTP 경로 재사용
  (SSRF/redirect/IP/deadline/키 보호), SSE `data:` 프레임 파싱→토큰 delta 누적→완성된
  NDJSON 줄만 의미 이벤트로, `[DONE]`, UTF-8 분할, 줄/총바이트/claim/문자/deadline/idle
  상한, 취소 시 즉시 close.
- `endpoint.py`에 취소 가능한 `stream_lines(...)` 추가(urllib incremental read + close, idle/
  deadline/size, redirect 차단, 정책 재검증). stdlib로 취소·close가 취약하지 않아 새 HTTP
  의존성은 도입하지 않는다(도입 시 §스펙3 조건 준수 필요 — 이번엔 불필요).

## 4. 검증 후 emit (`schema.verify` 재사용)

provider claim 이벤트마다: sourceChunkIds ⊆ 검색 subset & 현재 문서 소속 & 청크 미삭제·
미변경, 어휘 연결, 수치 원문 존재(자릿수 경계), 중복·빈 claim 제거 → **supported만**
`claim` NDJSON emit(서버가 저장 source_refs로 page/bbox 재구성). unsupported는 표시 안 함
(개수·비율만 구조화 로그). 최종 content는 **emit된 supported claim들을 서버가 조립**(모델
자유 answer 문자열을 최종 정답으로 신뢰하지 않음). 지원 0 → 검색 없음 not_found / 있음
insufficient / 유효 상충 conflicting. 4A 안전 문구 재사용.

## 5. 초안 저장

검증된 claim 텍스트만 `draft_content`에 체크포인트(이벤트마다 commit 금지 — 500ms 또는
1KB 이상 증가 시). draft는 최종 답변 아님. completed 시 최종 content·claims 저장 + draft
삭제를 같은 트랜잭션. cancelled/interrupted draft는 별도 상태로 남기되 기본 UI에서 정상
답변처럼 표시하지 않고, 이후 대화 문맥·질문 재작성에 사용하지 않는다.

## 6. API (`routes/qa.py` 확장)

- `POST .../messages/stream` → NDJSON. body `{question, retryOfMessageId?}`. 스트림 시작
  전 검증 오류는 AppError JSON, 시작 후 오류는 `error` NDJSON 이벤트. 첫 이벤트에 messageId·
  requestId.
- `POST .../messages/{message_id}/cancel` → 소유권 확인, streaming/finalizing만 취소, DB
  `cancel_requested_at` + 인메모리 cancel event, provider close, CAS(completed vs cancel).
  이미 completed면 completed, 이미 cancelled면 멱등, 타 문서/스레드 404.
- `GET .../messages/{message_id}/status` → 연결 끊긴 뒤 terminal 상태 확인(draft 원문 미포함).
- 4A 비스트림 API 유지.

## 7. 취소

프런트: AbortController + [취소] 버튼 → cancel API 먼저 → fetch abort. 이중 클릭 멱등,
취소 중 비활성, 중복 제출 차단. 백엔드: provider 읽기 반복·claim 검증 전후·final 직전 취소
확인(DB cancel_requested_at + 인메모리 event + `request.is_disconnected()`), provider close,
assistant `cancelled` CAS, 활성 잠금 해제. 취소된 답변은 정상 출처 답변처럼 표시 안 함.

## 8. 중단·복구

연결 끊기면 provider 무한 방치 금지 → 즉시 cancel 전달, status `interrupted`. sidecar 재시작
시 `recover_interrupted`에서 pending/streaming/finalizing assistant를 `interrupted`(error_code
`APP_RESTARTED`) 확정, interrupted_at 기록, 활성 잠금 해제, user 질문 보존, draft 승격 금지.
비정상 종료 후 DB 복구가 최종 안전장치.

## 9. revision·문서 변경 감지 (스트림 중)

시작 스냅샷(content_revision·chunk_revision·검색 subset hash) → 검색 직후·외부 호출 직전·
claim 검증 주기·finalizing 직전·최종 commit 직전·검색없음 저장 직전에 **최신 DB 재조회**
(identity-map 불신). 변경 시 provider 즉시 중단·이후 emit 금지·저장 금지, status
`revision_changed`, user 질문 보존, "문서 내용이 변경되어 답변을 다시 만들어야 합니다".
이미 보낸 draft claim은 최종으로 표시하지 않고 revision_changed로 교체.

## 10. 외부 동의 변경

외부 provider 이중 동의(user·document) 재검사: 시작·검색 후·외부 호출 직전·스트림 중 주기·
최종 직전. 철회 시 외부 연결 close·이후 처리 중단·status `consent_revoked`·draft 저장 안 함·
안전 문구. 이미 전송된 데이터는 되돌릴 수 없음을 문서에 명시(재전송·자동 재시도 안 함).
loopback 로컬은 동의 대상 아니나 endpoint/revision/취소 정책 동일.

## 11~12. 프런트

`useQaStream` 훅/상태머신: idle/connecting/retrieving/generating/finalizing/completed/
cancelling/cancelled/interrupted/failed. 요청별 requestId — 늦은 이전 요청 이벤트/abort/
completed 후 claim 무시, Strict Mode 중복 요청 방지, 동일 user message 중복 추가 금지.
fetch POST(X-MedBridge-Token 헤더, **EventSource 미사용**) + ReadableStream + TextDecoder
(stream) 라인 버퍼(UTF-8·다중 이벤트·미완결 마지막 줄 폐기). 점진 claim 버블·출처 배지
즉시·클릭 이동, 생성중/완료 시각 구분, 조건부 자동 스크롤, aria-live 과다 억제. 취소·중단·
revision·동의 UI. draft는 접힌 "중단된 미검증 초안"으로만. 탭 전환·언마운트 시 orphan
요청 방지(AbortController cleanup). 기술 용어(retrieval/chunk/stream/NDJSON/revision/bbox…)
비노출.

## 13. 프롬프트 인젝션 방어

청크=신뢰 불가 데이터. 시스템 프롬프트와 청크를 명확히 구분(문자열 단순 연결 금지, 구분
델리미터/역할 분리). "이전 지시 무시/키 출력" 등 문서 내 명령 무시, 문서 근거 외 행동·도구
실행·비밀 출력 금지 지시. API 키·endpoint·로컬 경로를 모델 입력에 넣지 않음.

## 14~17. 테스트·검증

스펙 §14 매트릭스(프로토콜/출처/취소/복구/revision/동의/동시성/프런트) + `docs/testing/
windows-sprint4b-streaming-validation.md`. 마이그레이션 0007↔0008 사이클, async 누수·미close
스트림 없음. 기존 테스트 무약화. Codex 독립 리뷰 후 수정.

## 제외 (Sprint 4C+)
다중 문서·외부 웹검색·음성·이미지·스트림 resume·다기기 동기화·서버 다중사용자·플래시카드·
문서 간 메모리·자동 의료 의사결정·도구 호출 agent.
