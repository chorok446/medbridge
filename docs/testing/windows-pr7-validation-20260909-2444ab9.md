# PR7 Windows 취소 수정 설치본 검증 — 2444ab9

> 판정: **출시 HOLD / PR Draft 유지**.
> 설치본의 취소 저장·후속 질문·강제 종료 복구·재시도·데이터 보존은 통과했다.
> 취소 직후 안내가 빈칸으로 남는 UI 갱신 경합을 별도로 발견해 수정했다.
> 아래 설치본과 실제 모델 평가는 UI 후속 수정 `ab02a09` 이전 코드의 결과다.
> 새 UI 수정의 installer 검증, 최종 고정 커밋 평가와 Windows 전체 검증은 남는다.

## 설치본과 백업 식별

- [CI 34345837520](https://github.com/chorok446/medbridge/actions/runs/34345837520):
  versions/backend/frontend/security/windows-build/summary 6개 job 모두 성공.
  완료 시각 2026-09-09 11:44:37 UTC.
- PR head: `2444ab906f908e4f22309fd5763205911e68138e`.
  취소 저장 수정: `0fc7eae4c973c5bb6e249f812c877377274d68d9`.
- manifest merge: `002c8510d90497b2e071a5405c3d00aa3fbb2e64`.
  merge와 PR head의 tree는 `7831b28b53b08de994e68b8038a013e3e00f419e`로 동일하다.
- artifact `10102151806`, `medbridge-windows-setup`.
  API digest: `sha256:a9828223a74800f8363f9f08b01cb3c4a01cd3f328e89fc5d54468b070fee777`.
- installer `MedBridge Study_0.1.0_x64-setup.exe`: 105,765,096 bytes.
  SHA-256: `2766348f3ddec54e4aa42d7d7f0c009bb3331657132fda57fa54f5db7488a683`.
  installer와 kit PDF 7개의 크기·해시를 manifest와 대조했다.
- Authenticode는 **NotSigned**다. 사용자에게 실행·설치 시점 승인을 받은 뒤
  기존 경로에서 컴포넌트 추가 및 재설치를 선택했다. 제거 옵션은 선택하지 않았다.
  설치 완료와 앱 실행을 확인했고 새 바탕화면 바로가기는 만들지 않았다.
  보안 경고를 우회하지 않았다. 깨끗한 비관리자 PC 최초 설치 검증은 아니다.
- 백업 `pr7-2444ab9-20260909-preupdate`: 11:58:43.3045593 UTC,
  93파일 / 9,209,197,821 bytes. 원본의 복사 전후·백업 해시가 모두 같았다.
  실행 직전 DB/WAL/SHM·설치 파일 등 핵심 80파일도 백업과 대조했다.
  사용자 DB·PDF·설정·Ollama를 삭제하거나 초기화하지 않았다.

설치 후 바이너리:

| 파일 | bytes | SHA-256 |
|---|---:|---|
| medbridge.exe | 15,316,480 | `cd6553f26d011b5a0fef982afb935863b905258a341af372bf77bf4a33a23f5c` |
| medbridge-sidecar.exe | 45,728,367 | `b67db85327e415a072ff5c541c02abd1400a2691592e4cfb8f1b3a6ac7b1f93e` |

## 실제 모델 평가 — 설치 전 고정 2444ab9

Windows Python 3.13.14 / local qwen3:8b. 시작·종료 commit/clean tree와
모델 digest를 고정하고 사용자 DB와 분리된 평가 DB·in-memory keyring을 사용했다.
알려진 개인 설정 `.codex/hooks.json` 외 작업 트리 예외를 추가하지 않았다.

```powershell
# apps/api
.\.venv\Scripts\python.exe -X utf8 scripts/evaluate_local_qa.py --model qwen3:8b --repeat 3 --fail-on-gate --output-dir artifacts/qa-evaluation/2444ab9-cancel-recheck
```

- 13케이스 × 3회 = **39/39 통과**, exit 0, `default_recommended`.
- 평가 기준상 위해 노출·안전 중요 실패·변동 0건. p50 2.985초 / p95 30.024초.
- 생성 시각 12:01:56 UTC. HTTP 500 한 번은 기존 native 재시도 2번째에 복구됐다.
  재시도 상한·평가 임계치를 바꾸지 않았고 폐기 claim/event는 0이었다.
- 모델 digest:
  `sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`.
- [전체 평가 JSON](qa-evaluation-20260909-2444ab9.json).
  실행 원본 SHA-256: `d5596f9ba7c56b4f83017ca3ccb5b55c53648094c518f368e37b218d9451e5b8`.
  저장소 LF 사본: `bbe340dcf968149da6e3a643de51a6bdb119c50c5ea5f11505947d73d16c944d`.
  줄바꿈 정규화 후 내용이 동일함을 확인했다. 평가 내용은 수정하지 않았다.

## 실제 GUI 시나리오

기존 `01_digital_simple.pdf`(2쪽 / 7,434 bytes)를 그대로 사용했다.
SHA-256: `191fb25f622107bd52154254c5c779e8a726b21f699e4e844d9815fd8e84ee7b`.
새 kit PDF로 교체하지 않았다. 각 쪽은 문단 1~12와
“심장은 온몸에 혈액을 보내는 근육 기관이다.”를 담은 합성 자료다.
Computer Use로 입력·중단·출처 이동·재시도를 조작하고 DB는 읽기 전용으로 검사했다.
아래 시각은 모두 2026-09-09 UTC다.

긴 질문: “심장은 온몸에 혈액을 보내는 근육 기관이다라는 문서의 문장을 구성하는
단어를 각각 설명하고 문단 1부터 문단 12까지 순서대로 원문을 인용해 주세요.”

| 동작 | 관측 | 판정 |
|---|---|---|
| 긴 질문 생성 중 실제 중단 버튼 | 12:13:36.772709 시작, 12:13:46.770182 취소 접수, 46.824399 확정. `cancelled / CANCELLED`, interrupted_at 없음, claim/draft/active 0 | 취소 저장 통과 |
| 같은 대화의 새 짧은 질문 | 12:14:57.848632 → 12:15:18.209026, 20.360초. `completed`, 원문에 맞는 supported claim 1개, 출처 1·2쪽, 중복 0 | 통과 |
| 짧은 답변의 2쪽 출처 클릭 | PDF 2/2로 이동, 원문 근거 영역 강조 | 통과 |
| 별도 대화의 긴 질문 | 12:17:48.149356 → 12:18:08.240767, 20.091초. 문단 1~12 원문·순서·출처, supported 12개, 중복 0 | 완료 응답 통과 |
| 다음 긴 질문 생성 중 강제 종료 | 12:19:54.710317 시작, 12:19:55.2457356 앱 프로세스 종료. 앱·sidecar 모두 없음. DB는 streaming 1개 | 강제 종료 재현 |
| 재실행 복구 | 12:20:21.781458 `interrupted / APP_RESTARTED`, claim/draft/active 0 | 통과 |
| 복구 화면의 다시 시도 | 같은 assistant 행에서 18.986초 후 completed. 문단 1~12 원문·순서·출처, supported 12개, 중복 0 | 통과 |

짧은 질문은 “이 문서에서 심장은 어떤 기관이라고 설명하나요?”다.
긴 질문의 문서 밖 단어 설명은 생성하지 않았으며, 원문 인용 완전성을 확인했다.
실제 내용 대조와 정확한 12개 목록·출처 검사를 수행했고 특정 금칙어 부재만으로
통과시키지 않았다. GUI 완료 긴 답변은 2회이며 모델 평가의 3/3과 구분한다.

첫 강제 종료 시도는 도구 승인·실행 전에 답변이 끝나 안전 가드가 종료를 거부했다.
이 실행은 강제 종료 성공으로 계산하지 않는다. 다음 시도는 60초 범위에서
정확한 설치 경로·PID, 합성 문서의 active Q&A 한 건, 다른 job/run의 활성 상태 부재를
확인한 뒤 앱만 강제 종료했다. Ollama나 다른 앱은 종료하지 않았다.
재시도가 복구 행을 갱신하기 전에 [별도 관측 JSON](windows-pr7-recovery-observation-20260909-2444ab9.json)을 저장했다.

화면 증거:

- [취소 직후 빈 안내](screenshots/pr7-2444ab9-cancel-empty.png)
- [후속 답변·취소 안내와 2쪽 출처 이동](screenshots/pr7-2444ab9-short-source.png)
- [복구 후 재시도 전](screenshots/pr7-2444ab9-recovery-before-retry.png)

## 새 결함: 취소 직후 UI 확정 상태 미갱신

취소는 DB에 올바르게 확정됐지만 화면에는 빈 답변 영역이 남았다.
새 질문을 완료해 대화를 재조회한 뒤에야 “답변을 중단했어요.”가 표시됐다.
이 UI 실패를 취소 저장 통과와 구분한다.

- `askQuestion`은 stream 종료 뒤 상세를 한 번 무효화한다. 클라이언트가 먼저
  연결을 끊으면 이 GET이 서버 finally의 DB 확정보다 빨라 빈 active 메시지를 받는다.
  이후에는 조회 계기가 없어 캐시가 그대로 남는다.
- 세 상태(pending/streaming/finalizing)를 반환하는 지연 확정 테스트에서
  취소 이후 추가 입력 없이 안내를 찾지 못하는 실패 **3개**를 재현했다.
  초기 테스트의 로딩 전 클릭 실패는 테스트 준비 오류로 수정했으며 제품 실패와 구분했다.
- `ab02a09`는 스트림이 끝났는데 상세에 active assistant가 남아 있을 때만
  500ms 간격으로 서버를 다시 조회한다. 서버의 terminal 결과·조회 오류·화면 이탈에서
  멈춘다. 스트림이 연결된 동안 중복 폴링하지 않는다.
- 임의의 시간 뒤 cancelled라고 표시하지 않으며 서버 확정 상태를 그대로 사용한다.
  API·DB·모델·근거 검증·사용자 이력은 바꾸지 않았다.
- 수정 후 **Web 30파일 / 220테스트 통과**, 타입 검사·린트·production 정적 빌드 통과.
  취소 경합 3개와 조회 오류·화면 이탈 2개 회귀를 포함한다.
  샌드박스의 Vitest spawn EPERM은 실행 환경 제한이므로 승인된 범위에서 다시 실행했다.
- 이 수정의 설치본 재검증은 아직 수행하지 않았다. 위 2444ab9 모델 평가도
  UI 후속 수정 커밋의 최종 평가를 대체하지 않는다.

## 데이터 보존과 정상 재실행

업데이트 직후 새 질문 전에 7개 테이블의 내용 해시가 백업과 동일했다.
검증 후 정상 종료 시 앱·sidecar가 모두 사라졌고, 다시 실행한 뒤에도 새 Q&A의
3개 테이블 해시가 종료 전과 같았다. 정상 종료와 강제 종료를 구분해 검사했다.

- 기존 documents 12, pages 4,339, blocks 364,819, summary_settings 1 행: 내용 해시 동일.
- 기존 QA threads 13/13, messages 52/52, claims 151/151: 각 행 전체 내용 보존.
- 이번 합성 검증만 증가: threads 15, messages 60, claims 176.
- quick_check `ok`, FK 위반 0, active Q&A 0. 모델 설정과 간호학생 선택 보존.
- [최종 답변·시각·원문 대조와 재실행 후 보존 수치](windows-pr7-answers-20260909-2444ab9.json).

## 남은 검증

새 UI 수정의 CI·installer·고정 커밋 평가, Windows 전체 36항목,
깨끗한 PC 설치/Ollama 미설치 경로, 오프라인·10회 연속·실제 300MB 이상
문서 부하·최종 패키지 메모리와 기본 창 패널 잘림 등은 이번 통과 범위가 아니다.
과거 크기 경계 벤치나 합성 문서 성공을 실제 대형 문서 전체 완료로 확대하지 않는다.
PR Ready 전환·병합·공개 릴리스·출시 승인 파일 생성은 하지 않았다.
