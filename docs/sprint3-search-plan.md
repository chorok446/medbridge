# Sprint 3A — 검색 기반 구현 계획

Sprint 3 전체 범위(검색·요약) 중 이번 작업은 **청크·검색 기반까지만** 다룬다.
요약 생성·모델 호출·대화형 Q&A(Sprint 4)는 범위 밖이다.

## 0. 현재 구조 조사 요약

- `Document`(문서), `DocumentPage`(페이지, `normalized_text`/`raw_text`/`requires_ocr`/`scan_verdict` 보유),
  `DocumentBlock`(`x0,y0,x1,y1`, `reading_order`, `is_header/footer/table/caption`, `block_type`),
  `DocumentWord`(`source_method`: `digital`|`ocr`) — Sprint 2/2B에서 이미 페이지·블록·단어 단위로
  좌표·출처가 저장돼 있다. 청크는 이 블록들을 reading_order 순으로 묶어 만든다.
- `DocumentJob`(`job_type`, `status`, `correlation_id`, `attempt_count` 등) + `LocalTaskRunner`
  (`apps/api/app/services/tasks/runner.py`) — `enqueue_*` 메서드로 asyncio task를 스폰하고,
  `recover_interrupted()`가 앱 재시작 시 QUEUED/RUNNING 잡을 정리한다. 청크 재생성도 같은 패턴을 따른다.
- 소유권 검사는 `app/services/documents/service.py:get_owned_document(db, user, document_id)` 단일 지점 —
  타인 문서는 항상 404(존재 노출 방지). 검색 라우트도 이 함수를 그대로 재사용한다.
- 오류 포맷은 `AppError(code, message, status_code, retryable, details)` → `_error_response`가
  `{error:{code,message,retryable,details}, meta:{correlationId}}`로 직렬화. 응답 성공은 `wrap(data)` →
  `{data, meta}`. 검색 API도 동일 포맷을 그대로 쓴다.
- 마이그레이션은 `alembic/versions/000N_*.py`, SQLite 호환을 위해 네이티브 enum 대신 `String`/`server_default`
  사용, 이번 것은 `0004_search.py`.
- SQLite는 이 환경(uv 관리 Python)에서 FTS5가 컴파일돼 있음을 직접 확인(`CREATE VIRTUAL TABLE ... USING fts5`
  성공). Windows CI 러너의 Python도 표준 배포판이라 FTS5가 포함될 것으로 예상하나, 실제 확인은
  windows-build 잡의 통과 여부로 검증한다(별도 언어 확장 설치 불필요, 실패 시 최우선 대응).

## 1. 청크 데이터 모델

`document_chunks` 테이블 (`app/models/search.py`):

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | Uuid PK | |
| document_id | FK→documents, CASCADE | |
| chunk_index | Integer | 문서 내 순서 |
| section_title | String, nullable | 청크가 속한 섹션 제목(있으면) |
| normalized_text | Text | 정규화된 청크 본문 |
| token_count | Integer | 안정적 길이 지표 — 토크나이저 의존 없이 `len(text)/4` 반올림으로 근사(문서화된 추정치) |
| page_start / page_end | Integer | |
| source_refs_json | JSON | `[{pageNumber, blockId, bbox:[x0,y0,x1,y1], readingOrder, sourceMethod}]` — 최소 1개 필수 |
| embedding_model | String, nullable | |
| embedding_dimension | Integer, nullable | |
| embedding_blob | LargeBinary, nullable | `array('f', vector).tobytes()` — numpy 미도입 |
| content_hash | String(64) | sha256(normalized_text) — 재실행 중복 생성 방지 |
| created_at / updated_at | DateTime | |

인덱스: `(document_id, chunk_index)`, `(document_id, content_hash)`.

**FTS5**: 별도 standalone 가상 테이블 `document_chunks_fts(normalized_text, section_title, chunk_id UNINDEXED, document_id UNINDEXED)`.
SQLite의 "external content" rowid 동기화 방식은 UUID PK와 맞물려 취약해지므로 채택하지 않는다 — 대신
청크 서비스가 `document_chunks` 쓰기와 **같은 트랜잭션**에서 FTS 행을 명시적으로 지우고 다시 넣는다
(이 코드베이스의 기존 관례: OCR 재실행도 트리거가 아니라 명시적 delete+insert). 개인용 앱 규모에서
본문 텍스트가 한 번 더 복제 저장되는 비용은 무시할 수준이다.

## 2. 청크 생성 정책 (`app/services/search/chunking.py`)

1. 페이지들의 `DocumentBlock`을 `(page_number, reading_order)` 순으로 나열(머리말/꼬리말 제외, 표는
   포함하되 별도 취급).
2. 제목(주로 `is_header`가 아닌 첫 큰 블록이나 명백한 섹션 제목 패턴)을 만나면 새 섹션 시작 —
   섹션 제목은 그 섹션의 `section_title`로 청크에 붙는다.
3. 블록을 순서대로 누적하며 목표 길이(문자 수 기준, `CHUNK_TARGET_CHARS`)에 도달하면 청크를 닫는다.
   표는 무조건 별도 청크로 분리(평탄화 금지 — 스펙 요구사항). 너무 길면 문장 경계(`. `, `. \n`, 한국어
   종결 어미 뒤 개행)에서 자르고, 그래도 안 되면 블록 경계에서 자른다.
4. 인접한 두 청크가 모두 `CHUNK_MIN_CHARS` 미만이고 같은 섹션이면 병합.
5. 페이지 경계를 넘어도 청크는 이어지되, **각 원문 조각(블록)의 page/bbox/reading_order/source_method는
   source_refs_json에 전부 남긴다** — 청크가 어느 페이지에서 왔는지 절대 잃지 않는다.
6. 디지털/OCR 중복: 같은 페이지에 digital 블록이 있으면 그 페이지의 OCR 전용 블록(`metadata_json.source
   == "ocr"`)은 청킹에서 제외 — Sprint 2B의 "디지털 우선" 정책과 동일.
7. `normalize_text()`(기존 `app/services/extraction/normalize.py`) 재사용 — 새 정규화 로직을 만들지 않는다.
8. `content_hash = sha256(normalized_text)`. 같은 해시의 청크가 이미 있으면(재실행) 새로 만들지 않고
   기존 행을 재사용 — 단, 위치/순서(source_refs, chunk_index)가 달라졌으면 갱신.
9. **재생성은 항상 원자적 교체**: 새 청크 집합을 계산한 뒤, 문서의 기존 `document_chunks`+FTS 행을 지우고
   새로 삽입 (Sprint 2의 페이지 재처리와 동일한 replace 패턴). 청크가 계속 늘어나며 누적되지 않는다.
10. 문서 삭제 시 `ondelete="CASCADE"`로 청크도 함께 삭제.

## 3. SQLite 키워드 검색

`app/services/search/keyword.py`: `document_chunks_fts`에 대해
`SELECT chunk_id, bm25(document_chunks_fts, 1.0, 2.5) AS raw_score FROM document_chunks_fts
WHERE document_chunks_fts MATCH ? AND document_id = ? ORDER BY raw_score LIMIT ?`
(`section_title` 가중치 2.5배 — bm25는 낮을수록 좋은 매치라 정규화 단계에서 부호를 뒤집는다).
빈 질의는 라우트에서 422(`VALIDATION_FAILED`)로 먼저 막는다. 소유권 검사는 라우트에서
`get_owned_document`로 처리 후 그 `document_id`만 필터링 — 다른 문서의 청크는 SQL 조건 자체에서 배제된다.

## 4. 임베딩 인터페이스 (`app/services/search/embedding.py`)

```python
class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int
    available: bool
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

- `DeterministicEmbeddingProvider(dimension=32)`: 텍스트의 sha256 해시를 시드로 한 결정적 벡터(테스트 전용,
  실제 의미 유사도 없음 — 순서·차원·저장/조회 로직 검증용).
- `DisabledEmbeddingProvider`: `available=False`. `embed_*` 호출 시 `RuntimeError` — 호출자는 항상 먼저
  `available`을 확인해야 한다(방어적으로 숨기지 않고 명시적으로 막는다).
- `get_embedding_provider()`: 설정(`Settings.embedding_provider`, 기본값 `"disabled"`)에 따라 선택. 실제
  상용 공급자는 이번 스프린트에 추가하지 않는다 — 하드코딩 금지 요구사항 그대로 반영.
- 문서에 `external_evidence_enabled`(기존 컬럼, 마스킹/외부 전송 동의 플래그)가 꺼져 있으면 임베딩을
  시도하지 않는다 — "개인정보가 검토되지 않은 문서를 외부 공급자로 보내지 않는다" 요구사항. 이번
  스프린트의 Deterministic/Disabled 공급자는 어차피 로컬 전용이라 실질적 위험은 없지만, 향후 실제 외부
  공급자가 붙을 자리에 이 가드를 미리 박아 둔다.

## 5. 벡터 검색 (`app/services/search/vector.py`)

- `ChunkVectorStore` Protocol(`get_candidates(document_id, model_name, dimension) -> list[(chunk_id, vector)]`) —
  SQLite 구현체 하나만 제공, 다른 저장소로 교체 가능하도록 분리.
- 임베딩이 없는 청크, 모델명/차원이 현재 질의 임베딩과 다른 청크는 후보에서 제외.
- 코사인 유사도는 순수 Python(`array`/`math`)으로 계산 — numpy 미도입, 개인용 앱 규모(문서당 청크 수백 개
  이하)에서 충분히 빠르다.
- NaN·빈 벡터·차원 불일치는 저장 시점(embedding 생성 서비스)과 조회 시점 양쪽에서 거부.

## 6. 하이브리드 검색 (`app/services/search/hybrid.py`, `app/services/search/settings.py`)

`SearchWeights`(상수 모듈, `thresholds.py`와 동일한 위치 관례):
`KEYWORD_WEIGHT`, `VECTOR_WEIGHT`, `SAME_PAGE_BONUS`, `ADJACENT_PAGE_BONUS`.

1. 키워드 점수(-bm25, min-max 정규화)와 벡터 점수(코사인, 이미 0~1 근사) 각각 계산.
2. `mode=hybrid`면 두 점수를 정규화 후 가중합. `mode=keyword`/`vector`는 해당 점수만.
3. 결과 병합 후, 이미 상위권에 든 청크와 같은/인접 페이지에 있는 다른 후보에 소폭 보너스.
4. 최종적으로 실제 사용된 `source_refs`만 응답에 포함(청크 전체가 아니라 매치에 실제 기여한 조각만 —
   1단계 구현에서는 청크의 `source_refs_json` 그대로 사용해 향후 부분 매치 축소 여지를 남긴다).
5. 원시 점수(bm25 값, 코사인 원값)는 API 응답에 노출하지 않는다 — 최종 정렬 순서만 반영.

## 7. API (`app/api/routes/search.py`)

- `POST /api/documents/{id}/chunks/rebuild` → 202, `{jobId, started}` (진행 중이면 `started=false`, 기존
  OCR `start_ocr`와 동일한 중복 실행 차단 패턴).
- `GET /api/documents/{id}/chunks/status` → `{chunkCount, lastRebuiltAt, jobStatus}`.
- `POST /api/documents/{id}/search` → 바디 `{query, mode="hybrid", limit=10}`. 빈 query → 422. 결과 0건 →
  200 + 빈 배열. 각 결과: `chunkId, preview(정규화 텍스트 앞부분), sectionTitle, pageStart, pageEnd,
  sourceRefs, matchType("keyword"|"vector"|"hybrid")`.

## 8. 비동기 작업 (`app/services/tasks/chunk_job.py`, `runner.py` 확장)

- `JobType.CHUNK_REBUILD` 추가(문자열 enum, 마이그레이션 불필요 — VARCHAR 컬럼).
- `enqueue_chunk_rebuild()` — OCR과 동일하게 최신 잡이 QUEUED/RUNNING이면 재실행 차단.
- 청크 재생성은 페이지 단위로 재개할 이유가 없는 짧은 배치 작업이므로, 크래시 시 재시도 가능하도록
  단순히 FAILED로 확정(OCR의 "stale job" 정리와 동일 원칙) — 사용자가 다시 [문서 검색 준비] 버튼을 누르면
  됨. `correlation_id`는 요청 미들웨어의 값을 그대로 사용(기존 관례).
- 진행 상태는 `DocumentJob` 행에 저장(기존과 동일), 원문 전체나 개인정보는 로그에 남기지 않는다(청크
  개수·소요시간만 구조화 로그).

## 9. 프런트엔드

`ExtractionReview`에 "검색" 탭 추가(기존 텍스트/구역별/표/안내 탭과 동일한 조건부 렌더링 패턴).
- 입력 + keyword/hybrid 토글, 로딩/빈 결과/오류(다시 시도) 상태.
- 결과 클릭 → 기존 `focusBlock`과 동일하게 `setPage`+`setHighlights`+`setFlashKey`로 PDF 뷰어 이동
  (검색 결과의 `sourceRefs[0]` 사용).
- 임베딩 비활성 시 hybrid 요청도 서버가 자동으로 keyword로 "위장"하지 않고, 프런트가
  `matchType`/서버가 내려주는 안내로 "의미 검색을 사용할 수 없어 단어 검색 결과만 표시합니다"를 명시.
- 포트·모델명·원시 점수 등 기술 정보는 어디에도 노출하지 않는다.

## 10. 테스트 계획

스펙 10번 항목을 그대로 백엔드 unit/integration, 프런트 vitest, 그리고 수동 E2E 체크리스트로 매핑한다
(상세는 구현 커밋의 테스트 파일 참고). Windows 빌드는 기존 CI(`ci.yml`)가 이미 sidecar 빌드+스모크
테스트를 포함하므로 별도 워크플로 변경 없이 같은 파이프라인으로 검증한다.

## 완료 기준 매핑

스펙 11번 항목을 그대로 준수. 구현 완료 후 별도 보고에서 항목별 충족 여부를 확인한다.
