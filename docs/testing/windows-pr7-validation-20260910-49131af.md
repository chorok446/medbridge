# PR7 Windows 취소 안내 자동 갱신 설치본 검증 — 49131af

> 판정: **출시 HOLD / PR Draft 유지**.
> 새 설치본에서 취소 안내 자동 표시·후속 질문·출처 이동·강제 종료 복구·재시도와
> 기존 데이터 보존을 확인했다. 고정 커밋 실제 모델 평가도 39/39 통과했다.
> 제품 코드는 변경하지 않았다. 아래 통과는 Windows 전체 36항목 통과가 아니다.

## 환경·설치본·백업

- 실행일: 2026-09-10 KST. 아래 상세 시각은 2026-09-09 UTC다.
- Windows 11 Pro 10.0.26200 / RAM 31.1 GiB / Ryzen 5 9600X.
  GPU: NVIDIA GeForce RTX 3080 및 AMD Radeon(TM) Graphics.
  앱 0.1.0 / Ollama 0.33.3 / local qwen3:8b / 학습 수준 간호학생.
  cold/warm 첫 로드는 별도 계측하지 않았다.
- [CI 34351682713](https://github.com/chorok446/medbridge/actions/runs/34351682713):
  backend/security/frontend/versions/windows-build/summary 6개 job 모두 성공.
  마지막 job 완료 12:49:43 UTC.
- PR head: `49131afa747c51d4e44005820e14cb4da00c2896`.
  UI 수정: `ab02a098ca66f71380e26dde2abc84d716b587b0`.
  manifest merge: `ef706a4a148e2a15e5befd0e11b8ff8594aff778`.
  merge/head의 tree는 `a86869283700f37253607c40d18c6b0919ebc257`로 같다.
- artifact `10104614011`, `medbridge-windows-setup`.
  API digest: `sha256:903176948cca36ab341ab0d8f4b3eb20312fb2f34dcafb5ba67c04b89ed2ba30`.
- installer `MedBridge Study_0.1.0_x64-setup.exe`: 105,754,280 bytes.
  SHA-256: `826d49d8e5d7b92e626dcfe5f8418d364b6014605cd6f6e630f9771ed2ad46a0`.
  installer와 kit PDF 7개의 크기·해시를 manifest와 대조했다.
- Authenticode **NotSigned**. 사용자에게 실행·설치 시점 승인을 받은 뒤
  기존 경로에서 컴포넌트 추가 및 재설치를 선택했다. 제거는 선택하지 않았다.
  설치 완료 후 앱을 실행했고 새 바탕화면 바로가기는 만들지 않았다.
  보안 경고 우회, 사용자 DB·PDF·설정·Ollama 삭제나 초기화는 없었다.
  기존 사용자 업데이트이며 깨끗한 비관리자 PC 최초 설치 검증은 아니다.
- 백업 `pr7-49131af-20260910-preupdate`: 18:11:57.0962104 UTC,
  93파일 / 9,209,206,803 bytes. 복사 전후 원본·백업 해시가 모두 같았다.
  실행 직전 18:19:02.3022385 UTC에도 93개 원본·백업 해시와 installer 해시,
  앱·sidecar 종료 상태를 다시 확인했다. 백업은 저장소 밖에 그대로 보존했다.

설치 후 바이너리:

| 파일 | bytes | SHA-256 |
|---|---:|---|
| medbridge.exe | 15,316,480 | `51b3a4fd0e817b2651ef1a7dbc564a49c26ea870a7d0e76887915d1bc423a478` |
| medbridge-sidecar.exe | 45,728,166 | `5578c5613ae5d9af6fe78bf414ae8114ff4d5c2dc6462b85d0e29f3e64f82806` |

## 설치 전 실제 모델 평가 — 고정 49131af

시작·종료 commit/clean tree와 모델 digest를 고정했다. 사용자 DB 대신
별도 평가 DB·in-memory keyring을 사용했고 개인 설정 `.codex/hooks.json` 외
작업 트리 예외를 추가하지 않았다.

```powershell
# apps/api
.\.venv\Scripts\python.exe -X utf8 scripts/evaluate_local_qa.py --model qwen3:8b --repeat 3 --fail-on-gate --output-dir artifacts/qa-evaluation/49131af-ui-refresh-rerun
```

- 첫 실행(`49131af-ui-refresh`)은 전체 평가 전 `connect_failed`, exit 1이었다.
  이후 Ollama version/tags와 같은 클라이언트의 준비 상태·모델 확인 10/10은 정상이었다.
  최초 연결 실패 원인은 확정하지 못했다. 성공 결과로 이 실패를 지우지 않는다.
- 코드·모델·타임아웃·평가 임계치를 바꾸지 않은 전체 재실행은
  **13케이스 × 3회 = 39/39 통과**, exit 0, `default_recommended`였다.
  평가 기준상 위해 노출·안전 중요 실패·변동 0건.
  p50 1.997초 / p95 19.862초, 생성 시각 18:13:35 UTC.
- 재실행 중 HTTP 500 한 번은 기존 재시도 정책의 2번째 시도에서 복구됐다.
  폐기 claim/event는 0이었다. 일시 연결 실패가 없었다고 해석하지 않는다.
- 모델 digest:
  `sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`.
- [전체 평가 JSON](qa-evaluation-20260910-49131af.json).
  실행 원본 SHA-256: `897a922ee6a57f677950d8106fff5d426e77c8cafcf5bd4849a14c9e747c190d`.
  저장소 LF 사본: `bc495e8744e2735b42798ba146951becd3852e830bea7c8a383b52ade5225ac9`.
  줄바꿈 정규화 후 내용이 동일함을 확인했으며 평가 내용은 수정하지 않았다.

## 실제 GUI 시나리오

기존 `01_digital_simple.pdf`(2쪽 / 7,434 bytes)를 그대로 사용했다.
SHA-256: `191fb25f622107bd52154254c5c779e8a726b21f699e4e844d9815fd8e84ee7b`.
각 쪽에 문단 1~12와 “심장은 온몸에 혈액을 보내는 근육 기관이다.”를 담은
합성 자료다. 새 kit PDF로 교체하지 않았다. Computer Use로 화면을 조작했으며
DB 검사에서는 읽기 전용 연결과 query_only를 사용했다.

긴 질문: “심장은 온몸에 혈액을 보내는 근육 기관이다라는 문서의 문장을 구성하는
단어를 각각 설명하고 문단 1부터 문단 12까지 순서대로 원문을 인용해 주세요.”

| 동작 | 관측 | 판정 |
|---|---|---|
| 생성 중 실제 중단 버튼 | 18:23:35.742110 시작, 46.812498 취소 접수, 46.866013 확정. cancelled / CANCELLED, interrupted_at 없음, claim/draft/active 0 | 취소 저장 통과 |
| 취소 후 추가 입력 없이 관찰 | 중단 직후 ‘중단하고 있어요…’, 다음 관찰에서 ‘답변을 중단했어요.’와 취소 내용 표시 | 자동 안내 통과 |
| 같은 대화의 새 짧은 질문 | 18:24:43.704263 → 18:25:03.444567, 19.740초. completed, supported claim 1개, 출처 1·2쪽, 중복 0 | 통과 |
| 짧은 답변의 2쪽 출처 클릭 | PDF 2/2 이동, 원문 근거 영역 강조 | 통과 |
| 새 대화의 긴 질문 생성 중 강제 종료 | 18:32:22.191194 시작, 22.2739944 앱 종료. 앱·sidecar 없음, DB streaming 1개 | 강제 종료 재현 |
| 재실행 후 복구 | 18:32:44.279170 interrupted / APP_RESTARTED, claim/draft/active 0 | 통과 |
| 복구 화면의 다시 시도 | 18:34:35.702919 → 18:35:01.540414, 25.837초. 같은 답변 행 completed, 문단 1~12·순서·출처·supported 12개·중복 0 | 통과 |

취소 버튼 클릭 기록은 18:23:46.607 UTC, 안내를 확인한 다음 화면 관찰은
18:23:59.489 UTC다. 그 사이 새 질문·수동 새로고침·포커스 전환 없이 안내가 나타났다.
관찰 간격은 실제 UI 갱신 지연 측정이 아니며 **500ms 내 표시를 계측한 것은 아니다**.
이전 2444ab9에서 나타난 빈 취소 안내가 이번 실행에서는 재현되지 않았다.

짧은 질문은 “이 문서에서 심장은 어떤 기관이라고 설명하나요?”다.
긴 답변은 문단 1~12를 실제 원문과 정확히 대조했다. 문서 밖 단어 설명은
생성하지 않았다. 이번 GUI의 완료 긴 답변은 **재시도 1회**이며 전체 모델 평가와
이전 설치본의 완료 횟수를 합산하지 않는다.

강제 종료 대기 가드의 첫 60초는 새 질문 없이 만료돼 아무 프로세스도 종료하지 않았다.
다음 시도는 정확한 설치 경로·PID, 새 합성 질문의 active Q&A 한 건과
다른 job/run의 활성 상태 부재를 확인한 뒤 MedBridge만 종료했다.
Ollama나 다른 앱은 종료하지 않았다. 재시도가 복구 행을 갱신하기 전에
[강제 종료 직후·재시작 후 관측 JSON](windows-pr7-recovery-observation-20260910-49131af.json)을 보존했다.

화면 증거는 합성 자료만 포함한다:

- [취소 안내 자동 표시](screenshots/pr7-49131af-cancel-notice.jpg)
- [후속 답변·2쪽 출처 강조](screenshots/pr7-49131af-short-source.jpg)
- [복구 후 재시도 전](screenshots/pr7-49131af-recovery-before-retry.jpg)

## 업데이트·정상 재실행 데이터 보존

설치 직후 새 질문 전에 7개 테이블의 내용 해시가 백업과 같았다.
검증 후 정상 종료를 별도로 수행했고 18:36:06.0716932 UTC 앱·sidecar 0개를
확인했다. 다시 실행한 뒤 기존 내용과 이번 테스트 결과도 그대로였다.

- 기존 documents 12, pages 4,339, blocks 364,819, summary_settings 1:
  전체 행 내용 해시가 백업과 동일하다.
- 기존 QA threads 15/15, messages 60/60, claims 176/176:
  각 행 전체 내용이 보존됐다.
- 합성 테스트 후 전체 QA threads 17, messages 66, claims 189.
  세 테이블의 정상 재실행 전후 해시가 모두 같다.
- quick_check `ok`, FK 위반 0, active Q&A 0. 모델 설정·간호학생 선택 보존.
- [최종 답변·시각·원문 대조·재실행 전후 해시](windows-pr7-answers-20260910-49131af.json).

## 남은 범위와 다음 작업

이번 기록은 UI 갱신 수정 설치본의 집중 회귀이며 제품 코드를 바꾸지 않았다.
웹 220개·타입·린트·빌드는 앞선 수정/CI의 결과로 구분하며 이번에 새로 실행한
검사로 집계하지 않는다. 문서 커밋 CI나 새 installer를 기다려야 다음 GUI 검증을
할 수 있는 상태는 아니다. 같은 설치본으로 비파괴 검증을 이어갈 수 있다.

- Windows 전체 [36항목 체크리스트](windows-sprint4c-qa-release-validation.md)는 미완료다.
  이번에 13, 20~25, 36의 해당 시나리오를 확인했다. 34는 프로세스 종료만 확인했으며
  외부 연결 잔류 검사를 포함한 항목 전체 완료로 올리지 않는다.
- 10회 연속 질문·오프라인·Ollama 중단 복구·최종 패키지 메모리 계측이 남는다.
- 깨끗한 PC/Ollama 미설치/최초 다운로드·취소는 이번 기존 사용자 업데이트로 대체하지 않는다.
- 실제 300MB 이상 문서의 전체 처리·Q&A 부하는 별도 수행해야 한다.
  과거 크기 경계 벤치나 2쪽 합성 성공을 실제 대형 문서 완료로 확대하지 않는다.
- 기본 창에서 질문 패널 오른쪽이 잘리는 기존 문제는 여전히 관찰됐다.
  최대화로 전체 안내를 확인했으며 레이아웃 수정은 이번 범위에 넣지 않았다.
- PR Ready 전환·병합·공개 릴리스·출시 승인 파일 생성은 하지 않았다.
