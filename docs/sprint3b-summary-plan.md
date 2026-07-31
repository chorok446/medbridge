# Sprint 3B — 출처 기반 구조화 요약 계획

Sprint 3A(검색 기반) 위에 문서 요약을 얹는다. 모든 요약 항목은 실제 청크와
PDF 페이지·좌표 출처에 연결되며, 모델이 만든 page/bbox는 신뢰하지 않는다.
대화형 Q&A(Sprint 4)는 구현하지 않는다.

## 0. 현재 구조 조사 요약 (기준 커밋 ccf18c6)

- **문서 모델**(`models/document.py`): `Document`에 revision 컬럼 없음. `external_evidence_enabled`(문서별 외부 처리 동의), `extraction_completed_at` 존재. `User`(`app_profile`)에 `external_ai_allowed`(전역 외부 AI 동의) 존재.
- **작업 실행기**(`services/tasks/runner.py`): 인프로세스 asyncio, `document_jobs` 행에 상태 저장, `recover_interrupted()`가 재시작 시 stale 잡을 FAILED로 확정. 동시성 가드는 각 서비스의 "최신 잡이 QUEUED/RUNNING이면 스킵"(체크-후-삽입) — DB 제약 없음.
- **OCR 완료 지점**: `ocr_job.py` 마지막 블록 + `ocr/service.py:rollup_document_status`. **현재 OCR 완료 후 청크 재생성을 자동 등록하지 않는다**(Sprint 3A Codex 리뷰에서 확인된 갭).
- **추출 완료 지점**: `tasks/extract.py:75-94`(`extraction_completed_at` 설정).
- **청크**(`models/search.py`): 청크마다 `content_hash`, `source_refs_json`(page/bbox/block/reading_order/source_method). 청크 세트 revision 추적 없음.
- **삭제**(`documents/service.py:delete_document`): soft delete라 FK cascade가 안 돌아 파생 데이터를 명시 삭제한다(pages→cascade, document_chunks/fts 명시). 요약도 여기에 추가해야 한다.
- **프런트**: `app/settings/page.tsx`(프로필/업데이트/문제해결 섹션 존재 — 요약 모델 설정 추가 지점), `app/documents/view/page.tsx`(메인 탭 "문서 보기"/"텍스트 확인" + 비활성 "요약" placeholder — 요약 탭 추가 지점). `extraction-review.tsx`가 PdfViewer+highlights 출처 이동 패턴의 원본.
- 마이그레이션 head: `0004_search` → 신규 `0005_summary`.

## 1. 공급자 구조 (`app/services/summary/`)

`EmbeddingProvider`와 완전히 분리한다. 요약은 벡터 검색에 의존하지 않고 청크의
`chunk_index`(reading order) + `section_title` 구조만으로 동작한다.

`provider.py`:
- `SummaryProvider` Protocol: `provider_name`, `model_name`, `available`, `supports_structured_output`, `summarize_group(request) -> GroupSummary`, `reduce_document(request) -> StructuredSummary`.
- `DisabledSummaryProvider`(기본, `available=False`, 호출 시 RuntimeError).
- `DeterministicSummaryProvider`(테스트 전용, 청크 텍스트에서 규칙 기반으로 구조화 출력 구성 — 실제 의미 요약 아님, 재현 가능).
- `OpenAICompatibleSummaryProvider`(httpx로 OpenAI 호환 chat/completions 호출, JSON 스키마 강제). endpoint/model/key를 코드에 하드코딩하지 않고 런타임 설정에서 읽는다. 실제 네트워크 호출은 CI에서 실행하지 않고 mock transport로만 단위 테스트한다.

`get_summary_provider()` 팩토리 — `ocr_service.engine()`·`get_embedding_provider()`와 동일한 단일 주입 지점(테스트 monkeypatch 가능). 설정(공급자 유형/endpoint/model/로컬 여부)은 DB(`app_profile` 확장 또는 신규 `summary_settings` 1행)에서, **API 키는 OS credential storage**에서 읽는다(§12).

외부 공급자 호출 전 게이트:
1. `provider.available` 확인 → 아니면 501 도메인 오류.
2. 로컬 공급자가 아니면 `document.external_evidence_enabled` AND `user.external_ai_allowed` 확인 → 아니면 명시적 동의 필요 오류.
3. 비밀값·문서 원문 전체를 로그에 남기지 않는다(청크 수·소요시간·토큰 총량만 구조화 로그).

## 2. 문서 revision과 무효화

`Document`에 두 컬럼 추가:
- `content_revision: int`(기본 1) — 추출 결과가 바뀔 때마다 증가.
- `chunk_revision: int | None` — 현재 청크 세트가 만들어진 시점의 content_revision.

무효화 규칙:
- **청크 stale** ⇔ `content_revision != chunk_revision`.
- **요약 stale** ⇔ 최신 성공 `summary_run.source_revision != content_revision`.

`content_revision += 1` 지점(전부 한 트랜잭션 안에서):
- 추출 완료(`extract.py`, EXTRACTED/PARTIALLY_EXTRACTED/OCR_REQUIRED 확정 시).
- OCR 완료(`ocr_job.py` 최종 블록, 실제로 페이지가 바뀐 경우).
- 추출 재실행(`retry_document` → 추출 완료 시 위 경로로 자동).

revision이 오르면:
1. 청크는 자동으로 stale(비교식으로 판정, 별도 플래그 불필요).
2. **청크 재생성 작업을 자동 등록**(OCR 완료 훅에 추가 — Sprint 3A 갭 보완).
3. 기존 요약은 자동으로 stale(비교식). 요약은 **자동 생성하지 않는다**(모델 호출은 사용자 실행).

상태·검색·요약 응답은 stale 여부를 명시한다. stale 요약을 최신처럼 보여주지 않는다.

## 3. 작업 동시성

`document_jobs`에 **부분 유니크 인덱스** 추가:
`CREATE UNIQUE INDEX uq_active_job_per_type ON document_jobs(document_id, job_type) WHERE status IN ('queued','running')`.
SQLite 부분 인덱스로 문서별·유형별 활성 잡 1개를 DB 레벨에서 보장한다(체크-후-삽입 경쟁 제거). 잡 삽입 4개 지점(추출/OCR/청크/요약)을 `IntegrityError` 처리로 감싸 "이미 진행 중"으로 응답한다.

> 검증 필요: 기존 validate/extract/OCR/chunk 흐름과 `recover_interrupted()`가 동일 유형 활성 잡을 이미 1개로 유지하는지 확인 후 인덱스를 켠다. 위반 사례가 있으면 해당 유형을 인덱스에서 제외하거나 흐름을 수정한다.

요약 결과 저장 게이트(revision-guarded commit):
- 시작 시 `source_revision = content_revision`, `source_chunk_hash = sha256(정렬된 청크 content_hash 목록)` 스냅샷.
- 저장 직전 document를 다시 읽어 `content_revision`과 청크 해시를 재계산.
- **둘 다 시작 시점과 같을 때만** artifact를 commit. 다르면 저장하지 않고 run을 `FAILED(REVISION_CHANGED)`로 확정 → 사용자 재실행 안내.

강제 종료 복구: `recover_interrupted()`에 RUNNING/QUEUED 요약 잡을 `FAILED(INTERRUPTED)`로 확정하는 블록 추가(OCR/청크와 동일 원칙). 사용자가 다시 실행 가능.

## 4. 요약 데이터 모델 (`0005_summary`)

`summary_runs`:
`id, document_id(FK CASCADE), status, provider_name, model_name, prompt_version, schema_version, source_revision, source_chunk_hash, learner_level, language, started_at, completed_at, error_code, created_at, updated_at`.

`summary_artifacts`:
`id, document_id(FK CASCADE), summary_run_id(FK CASCADE), artifact_type, title(nullable), position, content_json(JSON), source_chunk_ids_json(JSON), source_refs_json(JSON), created_at, updated_at`.

`artifact_type`(문자열 enum): `overview, section_summary, key_concept, prerequisite, important_number, target_population, learner_explanation, study_caution`.

- 모든 artifact는 chunk id ≥1개 + source ref ≥1개 필수. 출처 없는 artifact는 저장하지 않는다.
- 문서 삭제 시 run·artifact cascade 삭제(`delete_document`에 명시 삭제 추가 — soft delete라 FK가 안 돎).

## 5. 구조화 출력 스키마

공급자 출력은 자유 텍스트가 아니라 명시적 JSON 스키마(요청 프롬프트의 예시 형태:
`overview{text,sourceChunkIds}`, `sections[]`, `keyConcepts[]`, `prerequisites[]`,
`importantNumbers[]`, `targetPopulations[]`, `learnerExplanations[]`, `studyCautions[]`).

`schema.py`에서 파싱·검증:
- JSON 스키마 검증(형식·필수 필드).
- 알 수 없는 chunk id 거부, 현재 문서 소속 아닌 chunk id 거부.
- 빈 출처 항목 제거, 빈 내용 제거, 최대 길이 제한, 중복 정규화.
- **모델이 만든 page/bbox는 무시** — chunk id만 받아 서버가 저장된 `source_refs`를 재조회해 출처를 구성한다.

## 6. 요약 파이프라인 (`pipeline.py`)

한 번에 모델에 다 보내지 않는다:
1. **그룹 생성**(`grouping.py`): section_title 우선, reading order 유지, page 연속성 유지, 표–본문 관계 유지, 최대 입력 길이 제한, 너무 큰 섹션은 분할. 각 그룹은 원본 chunk id 목록을 들고 다닌다.
2. **map**: 그룹별 요약(공급자 `summarize_group`). 결과에 원본 chunk id 유지.
3. **reduce**: map 결과 + chunk id 연결을 입력으로 문서 전체 구조화 항목 생성(`reduce_document`). 최종 결과까지 원문 chunk id를 전달한다(중간 요약만 출처로 쓰지 않는다).
4. 구조화 항목 생성 → §5 검증 → §7 수치 검증.
5. §3 revision 게이트 통과 시 **원자적 저장**(run + 모든 artifact 한 트랜잭션).

## 7. 중요 수치·대상 집단 (`numbers.py`)

결정론적 전처리로 후보를 먼저 수집(정규식): 숫자+단위, 백분율, 연령, 기간, 용량,
기준 범위, 연구 대상 수, 포함·제외 조건. 모델은 후보를 정리·설명만 하고 없는 수치를
추가할 수 없다. **최종 수치 문자열이 실제 source chunk에 존재하는지 검증**(없으면 제거).
의료 권고·진단·임상 판단을 새로 생성하지 않는다.

## 8. 선수지식

항목: `concept, whyNeeded, sourceChunkIds`. `sourceType: document | general_background`.
이번 Sprint 기본 저장 대상은 `document` 근거 항목. 근거 없는 일반 배경지식은 문서
요약처럼 표시하지 않는다(별도 표시, 기본은 저장 제외).

## 9. 학습자 수준별 설명

수준 enum: `concise, nursing_student, experienced_nurse`. 기본 `nursing_student`.
모델에 환자 진단·처방·응급 판단을 요청하지 않는다. UI 고지 유지:
"이 내용은 학습 보조용이며 실제 환자의 진단·처방·응급 판단에 사용하지 마세요."

## 10. API (`app/api/routes/summary.py`, 기존 Envelope/오류 형식 유지)

- `POST /api/documents/{id}/summaries` → 요약 생성 시작(202). 바디 `{learnerLevel, language, includeSections, includePrerequisites}`.
- `GET /api/documents/{id}/summaries/status` → `{providerAvailable, status, stale, sourceRevision, currentRevision, progress, canRetry}`.
- `GET /api/documents/{id}/summaries` → 최신 run의 artifact 목록(출처 포함).
- `POST /api/documents/{id}/summaries/retry`.
- `POST /api/documents/{id}/summaries/cancel`.
- `DELETE /api/documents/{id}/summaries`.

공급자 비활성 시 501 또는 명시적 도메인 오류(앱 전체 실패 아님). 청크 미준비 시
"청크 준비 중" 상태 반환. 모든 라우트 `get_owned_document`로 소유권 검사.

## 11. 프런트엔드

문서 상세 화면(`documents/view/page.tsx`)에 메인 탭 "요약" 추가(기존 비활성
placeholder 대체). `SummaryView` 컴포넌트: 왼쪽 PdfViewer(highlights) + 오른쪽 요약.

상태: 아직 생성 안 함 / 모델 설정 필요 / 청크 준비 중 / 생성 중 / 완료 / 오래된 요약 / 실패 / 취소됨.
구성: 전체 개요 · 섹션별 요약 · 핵심 개념 · 선수지식 · 주요 수치·대상 · 학습자 설명 · 주의할 내용.
모든 항목에 출처 버튼 → 클릭 시 해당 PDF 페이지 이동 + bbox 하이라이트(`extraction-review`의
`setPage/setHighlights/setFlashKey` 패턴 재사용). 출처 여러 개면 목록 제공.

chunk id·모델명·token 수·원시 점수는 노출하지 않는다. 모델 미설정 시
"요약 기능을 사용하려면 앱 설정에서 요약 모델을 연결해 주세요." 안내.

## 12. 설정 UI (`app/settings/page.tsx` 확장 + API)

요약 모델 섹션: 사용 여부, 공급자 유형, endpoint, model name, API key 입력, 연결 확인,
로컬 모델 여부, 외부 전송 경고. 비밀 외 설정은 DB(`summary_settings` 1행), **API 키는
OS credential storage**(구현 방식은 아래 결정 사항 참조). 평문 DB·로그 금지.
외부 endpoint 선택 시 확인: "문서의 일부 내용이 선택한 모델 서비스로 전송될 수 있습니다."

## 13. 검색과 요약 연결

요약은 현재 문서의 전체 청크 스냅샷 사용. 사용자 검색 결과를 요약 입력으로 재사용하지
않는다. 요약 코드는 검색 API의 사용자 질의 상태에 의존하지 않는다(Sprint 4 Q&A 대비).
EmbeddingProvider 비활성이어도 청크 순서·구조로 요약 가능.

## 14. 테스트 / 15. 완료 기준

요청서 §14 매트릭스(백엔드/OCR·청크 연동/반복 문구 출처/프런트/E2E)와 §15 완료 기준을
그대로 매핑한다. 상세는 구현 커밋의 테스트 파일 참조. Windows 빌드는 기존 CI로 검증.

## 결정 사항 (구현 전 확인 필요)

- **API 키 저장 방식** (결정됨: Python `keyring`): sidecar에서 Python `keyring`
  패키지로 Windows Credential Manager에 직접 저장. Rust/IPC 변경 없음. 평문 DB·로그에
  키를 두지 않는다. CI·테스트는 in-memory keyring 백엔드를 주입해 실제 OS 저장소를
  건드리지 않는다. 키는 sidecar 프로세스 내에서만 읽고 API 응답·로그로 절대 반환하지
  않는다(설정 조회 시 "설정됨/미설정" 불리언만 노출).
