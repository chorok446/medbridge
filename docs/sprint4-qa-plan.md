# Sprint 4A — 출처 기반 문서 Q&A 계획

업로드된 단일 PDF를 근거로 질문하고, 답변의 각 주장(claim)을 실제 PDF 페이지·좌표
출처에 연결한다. **스트리밍·취소·중단 복구는 Sprint 4B로 분리**(이번엔 동기 JSON 응답).

## 0. 현재 구조 조사 요약 (기준 d3e395d, head revision 0005)

재사용할 자산:
- **검색**: `search/hybrid.py:search(session, doc_id, *, query, mode, limit, embedding_provider)`,
  `search/keyword.py`, `search/embedding.py:get_embedding_provider()`, `search/service.py`.
  `DocumentChunk`(normalized_text·source_refs_json·content_hash·chunk_index).
- **네트워크 안전**: `summary/endpoint.py`의 `validate_endpoint`·`assert_host_allowed`·
  `post_json`(HTTPS-only/loopback-only·DNS 재검증·redirect 차단·응답 크기 상한·전체
  데드라인)·`SummaryNetworkError`. **Q&A는 이 경로를 그대로 재사용**(새 HTTP 클라이언트 금지).
- **모델 설정·키**: `SummarySettings`(1행) + OS keyring. **Q&A 전용 저장소를 만들지 않고
  요약과 동일 설정·키를 재사용**.
- **revision·동의·검증 패턴**: `summary/service.py`의 `compute_chunk_hash`·
  `current_chunk_hash`·`provider_is_external`·`ensure_external_consent`,
  `summary/schema.py`의 `ChunkRef`·`_valid_ids`·`_refs_for`, `summary/numbers.py`의
  수치 원문 검증. Q&A용 검증 모듈에서 동일 원리로 재사용/모방.
- **문서**: `Document.content_revision`·`chunk_revision`, `delete_document`(soft delete라
  파생 데이터 명시 삭제 — 여기에 Q&A 테이블 정리 추가).
- **프런트 탭**: `app/documents/view/page.tsx` 메인 탭(preview/extraction/summary) — "질문"
  탭 추가. 출처 이동은 `extraction-review`/`summary-view`의 `setPage/setHighlights/
  setFlashKey` 패턴 재사용.

## 1. 데이터 모델 (마이그레이션 0006, down_revision "0005")

- `qa_threads`: id, document_id(FK CASCADE), user_id(FK CASCADE), title(nullable),
  created_at, updated_at, archived_at(nullable). 한 스레드 = 한 문서.
- `qa_messages`: id, thread_id(FK CASCADE), role(user|assistant), content(text),
  status(pending|completed|not_found|insufficient_evidence|conflicting_evidence|
  failed|revision_changed), sequence_number, document_revision, chunk_revision,
  provider_name(nullable), model_name(nullable), retrieval_mode(nullable),
  error_code(nullable), created_at, completed_at(nullable).
- `qa_claims`: id, message_id(FK CASCADE), claim_index, claim_text,
  verification_status(supported|unsupported|conflicting), source_chunk_ids_json,
  source_refs_json(스냅샷: pageNumber/blockId/bbox/readingOrder/sourceMethod/
  sectionTitle), created_at.

인덱스·제약:
- `uq_qa_messages_thread_seq` = (thread_id, sequence_number) 유일.
- `uq_qa_claims_message_index` = (message_id, claim_index) 유일.
- `ix_qa_threads_doc_updated` = (document_id, updated_at).
- **동시성**: `uq_qa_active_answer` = 부분 유니크 `(thread_id) WHERE role='assistant' AND
  status='pending'` — 스레드당 진행 중 답변 1개를 DB 레벨에서 보장.
- role·status는 문자열(SQLite 호환). soft delete 시 `delete_document`에서 명시 정리.
- 타 사용자/타 문서 접근은 404(존재 노출 금지).

## 2. Q&A 공급자 (`services/qa/provider.py`)

`QaProvider` Protocol: `provider_name`, `model_name`, `available`, `is_local`,
`answer(request) -> dict`(구조화 JSON). 구현: `DisabledQaProvider`,
`DeterministicQaProvider`(테스트 전용, 검색 청크에서 규칙 기반 답변·claim 생성),
`OpenAICompatibleQaProvider`(요약과 동일하게 `endpoint.post_json` 재사용, 비스트리밍
chat/completions). `get_qa_provider(session)` = `SummarySettings`+keyring로 해석
(요약과 동일 config). 테스트 override: config `qa_provider`(auto|disabled|deterministic).

## 3. 검색·컨텍스트 (`services/qa/context.py`)

질문마다 현재 문서 범위에서만 검색:
1. embedding 사용 가능 → hybrid, 아니면 keyword(폴백을 오류로 보이지 않게, 단
   `retrieval_mode` 메타데이터에 실제 모드 기록).
2. 결과 없음 → 모델 미호출(not_found).
3. 결과 chunk_id로 `DocumentChunk` 전체 normalized_text·source_refs 재조회.
4. 모델에는 chunkId·sectionTitle·text·pageStart/pageEnd만 전달(bbox·경로·DB 구조 비전달).
5. 프롬프트 크기 상한(문자 수) + 순위대로 절단, content_hash 기준 near-dup 제거,
   표 청크 markdown 유지.
6. 직전 대화(최근 N개·문자 상한)는 질문 해석용 문맥으로만, 근거 아님.

## 4. 모델 출력 계약·검증 (`services/qa/schema.py`)

모델은 JSON만: `{answer, answerStatus(answered|not_found|insufficient_evidence|
conflicting_evidence), claims:[{text, sourceChunkIds}], followUpSuggestions(≤3)}`.
서버 검증(모델 결과 그대로 저장 금지):
- sourceChunkIds ⊆ 이번 검색 청크 && 전부 현재 document_id 소속. 아니면 거부.
- 저장된 source_refs로 page/bbox 재구성(모델 값 무시).
- 빈/중복 claim 제거, 출처 없는 claim = unsupported, 수치는 원문 존재 검증.
- 지원 claim 0 → `insufficient_evidence`(검색 결과 있음) / `not_found`(검색 결과 없음).
- 모델이 상충 명시 + 양쪽 출처 유효 → `conflicting_evidence`.
- unsupported claim은 확정 사실처럼 저장/표시하지 않는다.

## 5. revision·동시성

질문 시작 시 `content_revision`·`chunk_revision`·검색 청크 (id,content_hash) 해시 스냅샷.
저장 직전 재확인 → 달라졌으면 저장 안 함: user 메시지는 보존, assistant는
`revision_changed`로 확정("문서 내용이 변경되어 답변을 다시 만들어야 합니다").
동시성: pending assistant 부분 유니크 인덱스로 스레드당 동시 생성 1개(체크-후-삽입 금지,
IntegrityError → 409). 재시도 멱등.

## 6. 개인정보·외부 전송

외부 공급자는 호출 시작 전 + 전송 직전 모두 `user.external_ai_allowed` AND
`doc.external_evidence_enabled` 확인(`ensure_external_consent` 재사용). loopback 로컬은
외부 전송 아님(단 endpoint 안전 검증 유지). 질문·청크·답변 원문을 로그에 남기지 않고
correlation/document/thread id·검색 결과 수·retrieval mode·status·소요시간·provider/model·
길이 정도만 구조화 로그.

## 7. API (`api/routes/qa.py`, 기존 Envelope/AppError)

- `POST /api/documents/{id}/qa/threads` 생성
- `GET /api/documents/{id}/qa/threads` 목록
- `GET /api/documents/{id}/qa/threads/{tid}` 메시지+claim+출처
- `PATCH /api/documents/{id}/qa/threads/{tid}` 제목/보관
- `DELETE /api/documents/{id}/qa/threads/{tid}` 삭제
- `POST /api/documents/{id}/qa/threads/{tid}/messages` 질문→검색→답변→검증→저장(동기 JSON)
- `POST /api/documents/{id}/qa/threads/{tid}/retry` 마지막 실패 재시도
빈 질문 422, 최대 길이·제어문자 검증, 없는/타 문서 thread 404. SSE 없음.

## 8. 프런트엔드 (`components/document-qa.tsx`, "질문" 탭)

첫 화면(예시 질문 3개)·입력·대화 UI·대기 표시·새 대화·목록·제목변경·삭제확인·재시도.
상태: 자료에 없음/근거 부족/상충/모델 미연결/외부 동의 필요/실패. 답변마다 claim 출처
배지 → 클릭 시 PDF 페이지·bbox 이동, "업로드한 문서 기준 답변"·비의료 고지. chunk id·
bbox 숫자·endpoint·내부 오류·status code·raw confidence 비노출. 중복 전송 방지. 모델
미연결이어도 열람·검색·요약 정상.

## 9. 삭제·보존

스레드 삭제 → 메시지·claim 함께 삭제. 문서 삭제 → 모든 Q&A 명시 삭제. 키 삭제는
Q&A 기록 유지. 공급자 변경 후 과거 답변은 provider/model·출처 스냅샷 유지.

## 10. 테스트 계획

스펙 §12 매트릭스(소유권/검색/출처/revision/동시성/동의·비밀/공급자/API) + 프런트
(렌더·전송·중복방지·상태별·출처 클릭·기술정보 비노출·hydration). 마이그레이션
0005↔0006 사이클. 기존 테스트 약화 금지.

## 제외 (Sprint 4B+)
스트리밍·취소·다중문서·외부 웹검색·음성·이미지·자동 진단/처방·문서 밖 일반지식·
플래시카드·대화 메모리 요약·다중 모델 라우팅.
