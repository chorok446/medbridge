# MedBridge 전체 기술 검토 및 개선 계획

> - 검토 기준일: 2026-08-13 (KST)
> - 최신 자동 검증일: 2026-09-12 (KST)
> - 기준 브랜치: `fix/summary-context-overflow`
> - 최초 검토 기준 커밋: `7d688b33e8a905311ebdc0d5a58feab0887c4663`
> - 마지막 제품 코드 검증 커밋: `df67118e2de58744dac3300ffbd01cf6f54c5d07`
> - 마지막 테스트 검증 커밋: `df67118e2de58744dac3300ffbd01cf6f54c5d07`
> - 마지막 평가 도구 검증 커밋: `a08b07763d7ef88d4d554eb368c092d693886d5c`
> - 구현 추적 브랜치: `fix/summary-context-overflow`
> - 문서 상태: 구현 추적 중. 자동 검증과 Windows 실기기 검증을 분리해 기록함.

## 1. 결론

현재 구현은 계층형 요약, 체크포인트 재사용, 출처 검증, Windows sidecar 수명주기,
오류 보고와 CI까지 상당히 보강되었다. 자동화 테스트도 대부분 통과한다.

다만 다음 두 판단은 분리해야 한다.

- **개발 및 추가 검증 지속:** 가능
- **300MB 이상 문서가 일반적인 Windows PC에서 안정적으로 처리된다고 보장:** 아직 어려움
- **공개 릴리스:** `HOLD` 유지 권장

최초 검토에서 가장 먼저 지적한 릴리스 승인 게이트, Q&A 안전 재시도, Rust lockfile,
대형 PDF 스트리밍 처리, 실데이터 형태 DB 마이그레이션 검증은 코드와 자동 테스트에
반영했다. 동일 콘텐츠 기반 300/500/800MiB 크기 경계와 중단 복구도 실측했다. 다만
보호된 `main`/Environment, 최종 installer의 실기기 36항목, 서로 다른 실제 대형 PDF
soak가 남아 있으므로 출시 `HOLD`를 유지한다.

### 2026-09-12 최신 Q&A 진행

- `df67118`에서 번호만 첫 줄에 있는 절 제목의 선행 본문 순서를 제한적으로 보정했다.
  위 여백·겹침 부재·바로 아래 전폭 본문을 확인하며 원문·좌표·신뢰도 0.5를 보존한다.
  새 회귀 **29 passed**, 기존 추출 포함 **79 passed**, 전체 재검증
  **1,520 passed / 4 skipped**, Ruff·mypy 통과다.
- 최초 전체 검사의 연결 오류 1건은 단독/관련 81개 및 전체 재검증에서 미재현이다.
  정확한 OS 원인은 미확정으로 기록했고 네트워크 코드나 검사 조건은 변경하지 않았다.
- 실제 저장 좌표에서 문제의 두 블록 순서는 바로잡혔다. 다만 줄바꿈 절 제목을
  청크 생성기가 제목으로 보지 않아 이전 표와 같은 청크에 남는 원인도 확인했다.
  다음은 검증된 절 경계를 청크 분리에 전달하는 일이며 최종 답변 누락은 미해결이다.
  사용자 데이터·설치본은 그대로다.
  [수정·최초 실패·보존·다음 작업](docs/testing/qa-section-order-20260912.md).
- `6910217`에서 짧은 번호 표 제목에 이전 표의 오른쪽 내용이 끼는 읽기 순서를 보정했다.
  새 합성 회귀 **23 passed**, 기존 추출 포함 **50 passed**, 전체 API
  **1,491 passed / 4 skipped**, Ruff·mypy 통과다. 경계를 끈 대조군은 9개 실패했다.
- 실제 두 페이지의 저장 좌표를 읽기 전용으로 재계산했다. 한 표의 제목과 I–IV 행은
  연속으로 묶였고 원문·좌표·신뢰도 0.5를 보존했다. 당시 남은 절 제목 문제는 위에서 보강했다.
  기존 사용자 문서·설정·대화는 그대로이며 설치본이나 활성 청크를 교체하지 않았다.
- 이후 격리된 자료 사본에서 청크 분리와 전체 청크/동일 질문을 비교해야 한다.
  새 코드의 고정 모델 평가·설치본 GUI는 아직 수행하지 않았다. 분류 완결성은 미해결이다.
  [원인·회귀·읽기 전용 확인·후속 범위](docs/testing/qa-table-caption-order-20260912.md).
- `a08b077`에서 **평가용 원문 선택 시험 경로**를 추가했다. 실제 앱에는 연결하지 않았다.
  새 테스트 **57 passed**, 전체 API **1,468 passed / 4 skipped**, Ruff·mypy 통과다.
- 모든 입력 번호의 포함/제외를 필수로 받고 원문·ID·출처를 그대로 보존한다.
  실제 질문 22회 비교/계측/최종 확인에서 형식 누락은 막았지만 분류 항목 누락은 남았다.
  최종 방식은 긴 질문 1개, 짧은 질문 2개 청크만 선택해 제품 적용을 보류한다.
- 당시 후속인 표 제목/행 순서 확인과 합성 재현은 위 `6910217`에서 부분적으로 수행했다.
  구조화 후보의 완결성은 남으며 원문 선택을 최종 답변 완료로 간주하지 않는다.
  [시험 구현·실패·후속 범위](docs/testing/qa-evidence-selection-prototype-20260912.md).
- `f7dfb5f`는 출처 수치 방어 회귀만 추가했다. 제품 동작은 변경하지 않았다.
  새 회귀 **8 passed**, 전체 API **1,411 passed / 4 skipped**, Ruff·mypy 통과다.
- 실제 원래 이력의 질문 8회에서 잘못된 출처 ID와 분류 항목 생략을 확인했다.
  수치가 검색된 다른 청크에 있어도 잘못 인용한 청크를 근거로 통과시키지 않는다.
  이 방어를 무력화한 별도 대조군은 4 failed / 4 passed였다.
- 프롬프트 대안 3개는 두 질문의 품질을 일관되게 개선하지 못해 모두 미채택했다.
  이후 위 평가용 원문 선택을 구현했으나 제품의 분류 완결성은 아직 해결하지 못했다.
  분류 완결성은 미해결이며 이번에 고정 전체 모델 평가 39회를 다시 실행하지 않았다.
  [출처 연결·누락 진단과 미채택 실험](docs/testing/qa-classification-source-diagnosis-20260912.md).
- `dde86c6`에서 설명과 인용을 섞은 상충 오탐과 주제어 중복 집계를 보강했다.
  당시 새 회귀 27개, 전체 API **1,403 passed / 4 skipped**, Ruff·mypy 통과다.
- 같은 실제 지원 claim 3개를 고정한 상충 오탐은 해소했다. 고정 전체 로컬 모델 평가는
  **39/39, default_recommended**지만 새 실제 답변은 각각 지원 claim 1개여서 불완전하다.
- 기능등급 I–IV 근거는 이미 검색에 포함됐다. 위 진단으로 답변 누락과 출처 오류를
  확인했지만 안정적인 개선은 아직 채택하지 않았다. 새 설치본 GUI도 남는다.
- 사용자 문서·설정·대화 해시 보존. 출시 HOLD와 PR Draft를 유지한다.
  [상세 기록](docs/testing/qa-conflict-scope-20260912.md),
  [현재 출시 검증 보고서](docs/testing/sprint4c-qa-release-report.md).

## 2. 확인된 검증 상태

### 2.1 구현·자동 검증 완료

- 릴리스 승인 gate fail-closed와 rename/schema 우회 회귀: `42 passed`
- 대형 PDF raw streaming 경로: API `77 passed, 1 skipped`, Web `188 passed`
- 청크 generation/O(1) 전환 및 소비 경로: 관련 회귀 `61 passed`
- 요약 provider identity/retry/동의/Windows checkpoint race: 전체 API
  `923 passed, 4 skipped` 시점 검증 및 관련 회귀 `285 passed`
- bounded evidence: 관련 회귀 `175 passed`, 5,000개 coverage 통합 검증 포함
- 동일 run 자동 재개/retention: 관련 회귀 `242 passed`
- HTTP slow-drip 절대 기한: endpoint/provider `145 passed`; 0.6초 제한 요청을
  정상 응답 약 0.62초, 오류 본문 약 0.82초에 중단
- 요약 협력 취소와 저장 직렬화: 당시 API `987 passed, 4 skipped`; endpoint
  `81 passed` 5회 연속, local AI/QA 관련 `44 passed`, Ruff와 mypy 통과
- Web 의존성 보안 패치: nanoid 3.x를 3.3.18로 고정; Web `211 passed`,
  frozen install, TypeScript, ESLint, Next production build 통과
- OCR 중 혼합 청크 활성화/검색 차단: `68 passed, 3 skipped`
- 웹 전체: `27 files, 211 passed`; TypeScript, ESLint, Next production build 통과
- Windows 검증 키트 bundle/provenance: `7 passed`
- Rust locked 검사를 CI에 추가: `fmt`, `check`, `test`, `clippy --locked`
- 보안 감사를 blocking job으로 전환하고 Windows build가 이를 의존하도록 변경
- 로컬 qwen3:8b 연결 검사: 관련 회귀 `64 passed`, 전체 API
  `988 passed, 4 skipped`
- Ollama native 스트림의 원자적 복구와 다국어 근거 계약: Q&A `196 passed`,
  전체 API `1019 passed, 4 skipped`
- 출시 Qwen 평가 provenance: 임시 DB/provider 격리, Git snapshot, 각 run의 tag와
  실제 loaded digest 검증을 포함한 qa_eval 회귀 `103 passed`

위 숫자는 각 원자적 변경 직후의 관련 회귀 기록이다. 벤치마크 기준 코드 커밋
`849fcd2`를 포함한 API 전체 회귀는 `1019 passed, 4 skipped`다. 이 SHA의 CI run
`32805755402`에서 backend, frontend, security, versions, Windows build가 모두
성공했다. installer artifact digest는
`sha256:21de8aeff12377e3ed2ccf1cd807ceb976ac687d127998f27d1fea0f44dd2152`다.
이후 문서와 릴리스 도구 변경이 추가되므로 이를 최종 release artifact로 재사용하지 않는다.
2026-09-08 당시 `35007a4`는 release gate `42 passed`, qa_eval `103 passed`, Ruff와 mypy를
통과했으며 당시 전체 API와 원격 CI는 다시 실행하기 전이었다. 최신 Q&A 검증은 위에 기록한다.

### 2.2 Windows 실기기 부분 검증 (2026-08-25)

아래 결과는 PR #7 통합 smoke다. 기존 사용자 데이터와 Ollama가 있는 Windows 실기기에서
수행했으며, 깨끗한 PC의 전체 36항목이나 공개 릴리스 승인을 뜻하지 않는다.

- GitHub Actions run `32781554276`의 `medbridge-windows-setup` artifact를 검증했다.
  manifest의 merge SHA `9bd07339b568688c33a4e30f6c44dcd2f8dcd3ef`의 두 번째
  parent가 당시 head `d2bbc4be98f7d141d4b316a6127c7c100b242268`임을 확인했다.
- installer SHA-256은
  `2f21722e9c3cf160d31ce33793da994c3a8606c33bea0f29dc90557bed18aaa2`이며,
  manifest와 installer 및 7개 테스트 PDF의 크기·SHA-256이 모두 일치했다.
- 앱 데이터 약 1.47GiB를 외부 경로에 해시 검증 백업한 뒤 installer를 재설치했다.
  DB는 Alembic `0015`, `quick_check=ok`, foreign key 위반 0건이었고 문서 11건과
  로컬 모델 설정이 유지됐다. 정상 종료 뒤 app/sidecar 프로세스 0건, 재실행도 통과했다.
- Ollama 프로세스를 중지하자 앱이 미실행 안내, 공식 URL
  `https://ollama.com/download/windows`, 주소 복사 경로를 표시했다. 설치 안내 버튼은
  제목이 `Download Ollama on Windows`인 Edge 창을 열었고, Ollama 재실행 뒤
  설치된 `qwen3:8b`를 다시 감지했다. Ollama 자체는 이미 설치돼 있었으므로 완전 미설치
  PC의 설치·모델 다운로드 검증으로 간주하지 않는다.
- 기존 연결 검사는 Windows cold start에서 30초 timeout이 발생했고, 32 tokens를
  reasoning에 소진해 빈 content를 반환하는 경우도 재현됐다. `bcce77c`에서 제한을
  60초, probe 예산을 128 tokens로 조정했다. 모델 unload 뒤 수정 payload는 8.02초,
  `finish_reason=stop`, content 6자, reasoning 0자로 성공했다. 별도 요약 모델 연결
  검사도 cold 상태에서 약 20초 안에 성공했다.
- 이미 등록된 411.6MB·1,060쪽 PDF를 열고 요약 진행률 10%→12%를 확인한 뒤
  취소했다. UI는 약 3초 안에 취소 완료를 표시했고 DB는 최신 run `CANCELLED`,
  활성 summary job 0건이었다. 이는 신규 업로드의 시간·peak RSS 벤치마크가 아니다.

`bcce77c` 대상 CI run `32786213277`의 완료 확인과 artifact 검증은 아직 대기 중이다.
또한 이 run은 이 문서 갱신 전 기준이므로 최종 HEAD artifact가 아니다. 이 문서까지
포함한 최종 artifact의 재설치·재검증도 대기 중이다. 따라서 위 installer SHA를 최종
`testedCommit`이나 공개 release 승인 자료로 재사용해서는 안 된다.

### 2.3 Windows 대형 PDF 크기 경계 실측 (2026-08-25)

세부 provenance와 주장 한계는
[`docs/testing/windows-large-pdf-size-envelope-benchmark.md`](docs/testing/windows-large-pdf-size-envelope-benchmark.md)에
기록했다.

- 동일한 2,173쪽 실제 PDF에 comment padding만 추가해 정확히 300/500/800MiB
  fixture를 만들었다. 이는 크기 경계 근거이며 서로 다른 콘텐츠 복잡도 근거가 아니다.
- 세 실행 모두 HTTP 201, 2,173개 고유 page row, 2,171쪽 `EXTRACTED`, 2쪽
  `OCR_REQUIRED`, blocks 308,991개로 정상 종결했다.
- 업로드/검증/추출/E2E는 각각 300MiB `2.538/3.611/1019.644/1027.232초`,
  500MiB `3.973/3.731/912.218/920.081초`, 800MiB
  `8.366/5.365/969.923/985.041초`였다.
- OS peak working set은 세 크기 모두 약 2.25GiB였고 DB+WAL 증가는
  265.95~268.04MiB였다. 100ms sample 오류는 0건이었다.
- 세 정상 재기동은 `recovered_jobs=0`이었다. 별도 300MiB 중단 fixture는 같은
  문서/job이 attempt 1에서 attempt 2로 복구돼 2,173쪽을 완성했고, 다음 재기동은
  다시 `recovered_jobs=0`이었다.
- 모든 최종 DB는 Alembic `0015`, `quick_check=ok`, foreign key 위반 0건,
  활성 job 0건이었다.

사전 성능 SLA가 없었으므로 SLA 통과를 주장하지 않는다. source Uvicorn 측정이므로
packaged sidecar 메모리와 Windows GUI 36항목도 별도로 남아 있다.

### 2.4 아직 자동 테스트로 대체할 수 없는 항목

- 새 installer를 설치한 깨끗한 Windows PC에서 Ollama 미설치 안내부터 모델 활성화,
  앱 재시작, Q&A/요약, 제거까지 수행하는 실기기 검증
- 실제 qwen3:8b 전체 평가 3회와 안전/변동 gate
- 서로 다른 실제 300MiB, 500MiB, 800MiB PDF의 full extraction soak
- 실제 사용자 DB 사본의 장시간 migration, backup 복원 및 실패 훈련
- 검증한 RC installer와 공개 installer의 SHA-256 동일성 확인
- GitHub의 보호된 `main`, required review/ruleset, release Environment reviewer 설정

### 2.5 원자적 변경 기록

- 릴리스/운영: `a2e10b6` 승인 gate, `cf69b9b` rename/schema 우회 차단,
  `35007a4` 출시 평가 provenance, `35f1d33` Windows kit provenance,
  `9f732c6` blocking 보안 감사
- 데이터 보존: `6665f09` migration fixture·디스크 사전 점검,
  `00b6284` 단일 스트림 업로드, `8232572` 청크 generation,
  `0aa9770` OCR/청크 interleaving 차단
- Q&A/설정: `c635f40` 안전한 스트림 재시도, `34237f8` thread·설정 UX,
  `9bae435` 삭제 대상·공백 키 경합 차단, `7d2ad1f` 다국어 근거 보존,
  `849fcd2` Ollama native 원자적 스트림 복구
- 요약: `7936eaa` provider identity·retry·동의, `8abd75b` bounded evidence,
  `8fe3c5e` HTTP 절대 기한, `2cc3fbf` 동일 run 자동 재개·retention,
  `05a09f3` 요청별 HTTP 중단, `2423eba` 취소/저장 CAS,
  `87a98df` pull/Q&A 스트림 취소·기한
- Web build: `b4d743f` nanoid 3.3.18 advisory 패치
- 의존성/로컬 AI: `d2bbc4b` pypdf 6.16.2 보안 하한,
  `bcce77c` qwen3 cold 연결 검사
- Desktop/시작·종료: `4ca964b` Rust locked CI, `027ae44` bounded quiescence,
  `ef99c8c` sidecar 지속 감시, `2b8fb0d` Ollama 설치 안내 복구
- UI/운영: `db36a67` 저장 요약·pagination, `2ab2a5b` 로그 회전·redaction
- 저장소 위생: `3df0402` LF/CRLF 정책, `20cb062` Python format-only 정규화

각 항목의 SHA는 해당 변경이 검증된 시점을 가리킨다. 이후 코드 커밋이 추가됐으므로
이 SHA들을 출시 `testedCommit`이나 과거 artifact 재사용 근거로 사용해서는 안 된다.

## 3. P0 - 릴리스 전 필수 개선

이하의 `근거`, `문제`, `현재 상태`, `개선안`은 최초 기준 커밋에서 발견한 내용을
결정 이력으로 보존한 것이다. 현재 코드 반영 여부는 각 제목 바로 아래의 상태 줄과
2.5 변경 기록을 기준으로 판단한다.

### P0-1. 릴리스 승인 게이트 우회 차단

**상태: 코드 완료 (`a2e10b6`, `cf69b9b`, `35007a4`) /
실제 승인 자료 없음으로 HOLD**

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

**상태: 외부 저장소 설정 필요 — 차단 상태 유지**

검토 시점의 원격 저장소에는 `main` 브랜치가 없고 `develop` 보호 규칙과 repository
ruleset도 확인되지 않았다. 현재 PR은 Draft이며 사람 리뷰 결정도 없다.

**개선안**

- `main` 브랜치를 만들고 required CI, 최신 base 반영, 최소 1명 리뷰를 강제한다.
- 공개 릴리스는 보호된 `main` 또는 서명된 `v*` 태그에서만 실행한다.
- 외부 릴리스 저장소 PAT는 보호된 Environment secret으로 이동한다.
- `contents: write` 권한은 게시 단계에만 최소 범위로 부여한다.

### P0-3. Q&A 재시도 계약 및 근거 안전성 통일

**상태: 완료 (`c635f40`, 후속 UI 경합 `34237f8`, `9bae435`)**

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

**상태: CI 코드 완료 (`4ca964b`) / run `32781554276` 설치·종료·재실행 smoke 통과 /
최종 HEAD 36항목 대기**

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

**상태: 자동 검증 부분 완료 (`6665f09`) / 구 artifact DB `0015`·`quick_check=ok`·
FK 위반 0건·재기동 통과 / backup 복원·최종 installer 대기**

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

**상태: 코드 완료 (`00b6284`) / same-content 300·500·800MiB 실측 완료 /
서로 다른 실제 대형 문서 soak `HOLD`**

**현재 상태**

- `apps/api/app/services/documents/service.py:32-46`은 업로드 전체를 `bytearray`에 누적한다.
- `apps/api/app/services/documents/storage.py:67-68`은 저장된 원본을 `read_bytes()`로 읽는다.
- `apps/api/app/services/documents/validation.py:46-79`는 전체 bytes를 `BytesIO`로 감싼다.

300-800MiB 문서는 파일 크기 외에도 Python 객체, PDF parser, OCR의 메모리가 추가된다.
저사양 PC에서는 pagefile thrashing이나 OOM 가능성이 있다.

**개선안**

- 같은 저장 디렉터리의 임시 파일로 업로드를 스트리밍한다.
- 스트리밍 중 크기 제한, `%PDF` signature, SHA-256을 증분 계산한다.
- 저장 완료 후 `fsync`와 원자적 rename을 사용한다.
- PDF parser에는 경로 또는 파일 handle을 전달한다.
- 300MiB, 500MiB, 800MiB fixture로 peak RSS와 소요시간을 기록한다.
- 동일 콘텐츠 padding fixture와 서로 다른 실제 대형 문서 soak의 주장을 분리한다.

### P1-2. 청크 재생성의 메모리와 SQLite writer lock 축소

**상태: 완료 (`8232572`), OCR 동시성 후속 보강 (`0aa9770`)**

`apps/api/app/services/search/chunking.py:349-444`은 문서 전체의 page, block, table,
draft와 ORM row를 메모리에 구성하고, 한 트랜잭션에서 청크별 FTS insert를 수행한다.

**개선안**

- FTS insert를 `executemany` 또는 bounded batch로 변경한다.
- 새 chunk generation을 shadow 영역에 작성한다.
- 완료 시 active generation만 원자적으로 전환한다.
- 이전 generation은 별도 정리 작업으로 삭제한다.
- 계획 시점의 document revision을 저장하고 전환 직전에 다시 검사한다.

### P1-3. 요약 출처 크기를 문서 전체 크기와 분리

**상태: 완료 (`8abd75b`)**

`apps/api/app/services/summary/executor.py:316-352`은 상위 노드의 출처를 모든 자식
chunk ID의 합집합으로 만든다. 최상위 artifact 하나에 수천 개 bbox가 붙을 수 있고,
여러 artifact에서 반복되면 DB와 API 응답이 크게 증가한다.

**개선안**

- 내부 범위인 `coverage_chunk_ids`와 사용자에게 보여줄 `evidence_chunk_ids`를 분리한다.
- reduce 단계에서 검증된 대표 근거만 제한된 수로 유지한다.
- 상세 출처는 페이지 범위, lazy loading 또는 pagination으로 제공한다.

### P1-4. 진짜 자동 재개와 일시적 오류 재시도

**상태: 완료 (`7936eaa`, `8fe3c5e`, `2cc3fbf`, `05a09f3`, `2423eba`, `87a98df`)**

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

**상태: 완료 (`7936eaa`, `2cc3fbf`)**

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

**상태: 완료 (`7936eaa`)**

외부 provider 사용 동의는 작업 시작 시 검사하지만, 장시간 요약의 각 네트워크 호출 직전에
최신 동의 및 provider 설정을 다시 확인하지 않는다. 실행 중 동의를 철회해도 남은 청크가
계속 전송될 수 있다.

**개선안**

- 각 외부 네트워크 호출 직전에 document/user 동의와 provider fingerprint를 새로 읽는다.
- 동의 철회 또는 provider 비활성화 시 활성 외부 작업을 취소한다.
- 첫 map 호출 후 동의를 철회했을 때 두 번째 요청이 전송되지 않는 테스트를 추가한다.

### P1-7. 종료, 업데이트, sidecar 장애 처리

**상태: 부분 완료 (`027ae44`, `ef99c8c`) — 제한된 자동 재시작은 미구현**

- `apps/api/app/services/system/runtime.py:135-146`은 deadline을 계산하지만 내부
  `runner.drain()`이 장시간 block되면 timeout을 확인하지 못한다.
- `apps/desktop/src-tauri/src/lib.rs:480`의 감시 thread는 최초 readiness 확인 후 종료한다.
- 대형 작업 중 sidecar가 OOM 또는 crash로 종료되어도 앱은 계속 ready로 보일 수 있다.

**개선안**

- `asyncio.wait_for` 또는 task 상태 polling으로 quiescence 시간을 실제로 제한한다.
- 종료 시 새 작업 차단, 취소 신호, 짧은 checkpoint grace period, 강제 중단 순서로 처리한다.
- `Child::try_wait()` 기반 지속 감시와 `sidecar-failed` 이벤트를 추가한다.
- 제한된 자동 재시작과 전역 복구 화면을 구현한다.

### P1-8. 취소 뒤 네트워크와 저장 작업의 즉시 정지

**상태: 완료 (`05a09f3`, `2423eba`, `87a98df`)**

장시간 요약을 취소해도 동기 HTTP worker가 제한 시간까지 남거나, 취소 commit 뒤 늦게
도착한 모델 결과가 체크포인트·artifact를 다시 저장할 수 있었다. Ollama pull과 Q&A
스트림도 `read(8192)` 안에서 멈추면 취소와 전체 기한을 다음 읽기 경계까지 확인하지 못했다.

- job/run별 취소 신호를 provider worker, Ollama metadata 조회, 실제 HTTP socket에 연결했다.
- 실행 시작·실패·체크포인트·최종 저장의 첫 쓰기를 active job CAS로 직렬화했다.
- 취소·삭제가 먼저 commit되면 늦은 worker는 checkpoint와 artifact를 저장하지 않는다.
- 연결, 응답 header, body, HTTP 오류 본문을 같은 절대 기한과 socket abort로 제한했다.
- pull/Q&A 스트림은 `read1` 경계에서 취소·idle·전체 기한을 확인하고, 취소는 오류가 아닌
  정상 iterator 종료로 처리한다.
- 이미 취소된 스트림은 DNS 조회 전 종료하고, 줄바꿈 없는 입력도 64KiB 줄 상한에서
  즉시 거부한다. 실행 중 운영체제 DNS 호출 자체는 공개 handle이 없어 OS 상한에 의존한다.

## 5. P1/P2 - 사용자 경험 및 운영 개선

### 5.1 요약 화면

**상태: 완료 (`db36a67`)**

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

**상태: 완료 (`db36a67`)**

- `apps/web/app/documents/page.tsx:26`은 `listDocuments()`를 한 번만 호출한다.
- 백엔드 기본 limit은 20이고 client type에는 이미 `nextCursor`가 있다.

**개선안**

- `useInfiniteQuery` 또는 `더 보기`를 구현한다.
- 업로드, 삭제, 검색 변경 시 page merge와 cursor reset을 테스트한다.

### 5.3 Q&A thread 전환

**상태: 완료 (`34237f8`, 삭제 대상 경합 후속 `9bae435`)**

thread 목록이 아직 로딩 중일 때 질문하면 기존 thread가 있어도 새 thread가 생성될 수 있다.
스트리밍 중에도 thread 선택, 새 대화, 삭제가 가능해 표시 thread와 실행 중 stream이 어긋날 수 있다.

**개선안**

- 목록 로딩 완료 전 질문 입력과 예시 질문을 잠근다.
- 스트리밍 중 thread 전환은 취소 확인 후 수행한다.
- 목록/상세 조회 오류와 빈 thread 상태를 구분한다.

### 5.4 설정 화면

**상태: 완료 (`34237f8`, 공백 API key 후속 `9bae435`)**

- 연결 확인이 수정 중인 draft가 아니라 저장된 설정을 검사한다.
- 저장된 API key를 UI에서 삭제할 수 없다.

**개선안**

- draft 설정을 전달해 검사하거나 저장 후 검사한다.
- 기존 delete API를 UI에 연결하고 명시적 키 삭제 동작을 제공한다.

### 5.5 로그와 오류 보고서

**상태: 완료 (`2ab2a5b`)**

- `apps/api/app/core/logging.py`는 회전 없는 `FileHandler`를 사용한다.
- OCR stderr와 예외 문자열에 Windows 사용자명, temp 경로, 문서명이 포함될 수 있다.
- sidecar bootstrap stdout/stderr가 폐기되어 시작 실패 원인을 찾기 어렵다.

**개선안**

- `RotatingFileHandler`와 크기/세대 제한을 적용한다.
- home, app data, temp, resource 경로를 구조적으로 redaction한다.
- 원문 stderr 대신 단계, exit code, 분류된 오류, 최근 sanitised 로그만 보고서에 포함한다.

### 5.6 Ollama 미설치 안내

**상태: 자동 검증 완료 (`2b8fb0d`) / 미실행 smoke 통과 / 완전 미설치 PC 대기**

- Tauri opener capability를 공식 Windows 다운로드 URL 하나로 제한한다.
- opener 거부와 브라우저 popup 차단을 UI에서 숨기지 않고 실패 안내로 표시한다.
- 설치 페이지를 열지 못해도 항상 공식 URL을 표시하고 복사/수동 입력 경로를 제공한다.
- Ollama 프로세스 중지 상태에서 안내, 공식 URL, Edge 창 열기, 재실행 뒤 모델 재감지는
  실기기에서 통과했다.
- 실제 Ollama 미설치 PC의 설치와 모델 다운로드·취소·중복 차단은 Windows 36항목으로
  남긴다.

## 6. 구현 권장 순서

아래 순서는 최초 검토 당시의 권장 순서다. 코드 항목은 작은 커밋 단위로 대부분 반영했으며,
외부 설정·실기기 항목은 완료로 오인하지 않도록 그대로 남긴다.

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
- 서로 다른 실제 300MiB, 500MiB, 800MiB 문서 soak test
- 업데이트, 강제 종료, 디스크 부족, 네트워크 중단 복구 시험

## 7. 출시 판단 체크리스트

아래 항목이 모두 충족되기 전까지 공개 릴리스는 `HOLD`로 본다.

- [x] 승인 게이트가 `HEAD`, 빈 artifact, 과거 artifact 재사용을 거부한다.
- [ ] 보호된 main/tag와 Environment 승인에서만 공개 릴리스가 가능하다.
- [ ] `cargo check/test/clippy --locked`가 최종 HEAD의 Windows CI에서 통과한다.
- [ ] 실제 사용자 규모 DB 사본의 0013 migration과 backup restore가 검증됐다.
- [ ] 검증 installer와 공개 installer의 SHA-256이 동일하다.
- [x] 300MB 이상 PDF 업로드 경로가 파일 전체를 Python heap에 올리지 않는 구조다.
- [x] 청크 생성이 bounded batch와 shadow generation/O(1) 포인터 전환으로 동작한다.
- [x] 앱 재시작 후 조건이 같은 장시간 요약이 같은 run/job에서 자동 재개된다.
- [x] transient provider 오류가 실제 HTTP 경계의 bounded retry로 처리된다.
- [x] 외부 AI 동의 철회 후 다음 실제 모델 전송을 시작하지 않는다.
- [x] 취소가 먼저 확정되면 늦은 요약 결과가 저장되지 않고 활성 socket이 중단된다.
- [x] sidecar crash를 감지하고 전역 복구 화면으로 전환한다.
- [x] Q&A 최종 본문에는 서버가 검증한 supported/conflicting claim만 표시된다.
- [x] 동일 콘텐츠 기반 300/500/800MiB fixture의 시간, peak RAM, DB/WAL,
  정상 재기동과 중단 복구 결과가 기록됐다.
- [ ] 서로 다른 실제 300/500/800MiB PDF의 full extraction soak가 통과했다.

### 7.1 현재 출시 차단 사유

1. 원격 저장소에 보호된 `main`과 release Environment required reviewer가 없다.
2. PR artifact의 설치·재시작·Ollama 미실행·411.6MB 요약 취소 smoke만 통과했다.
   최종 HEAD installer의 Windows 36항목 전체 결과는 없다.
3. 최종 HEAD의 qwen3:8b repeat=3 평가 artifact가 없다.
4. 동일 콘텐츠 300/500/800MiB 크기 경계는 실측했지만 서로 다른 실제 대형 PDF
   soak와 사용자 DB 사본 복구 훈련이 없다.
5. 따라서 `docs/testing/release-approval.json`은 존재하지 않으며 생성해서도 안 된다.

## 8. 문서 유지 규칙

- 코드 수정이 완료되면 관련 항목에 PR/commit과 검증 결과를 추가한다.
- 자동 테스트 통과와 실기기 검증 통과를 별도 상태로 기록한다.
- 동적 정보인 CI run, HEAD, installer hash, 모델 digest는 날짜와 함께 갱신한다.
- 발견 사항을 삭제하지 말고 `완료`, `수용`, `연기` 중 하나로 상태를 전환해 결정 이력을 남긴다.
