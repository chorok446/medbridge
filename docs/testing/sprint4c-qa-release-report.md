# Sprint 4C-B — 로컬 Q&A 출시 판정 보고서

> **출시 판정: 보류 (HOLD)** — 실제 qwen3 모델 평가(Layer 2)와 Windows 실기기 검증
> (Layer 3)이 아직 수행되지 않았다. 이 보고서는 허위 결과를 담지 않는다.

## 2026-09-08 PR #7 재검증 — 콜드 시작 실패와 수정

- `bf545b1`의 qwen3:8b repeat=3 평가가 첫 케이스에서 중단됐다.
  Ollama 0.33.3의 `/api/chat`은 약 15.25초에 클라이언트 연결이 끊겼고,
  모델 초기화가 취소되어 `/api/ps`가 비어 있었다. provenance 검증은
  fail-closed로 종료했으며 출시 평가 artifact를 생성하지 않았다.
- 원인: `urllib.open()`이 응답 헤더까지 기다리는데도 Q&A의 15초 연결
  예산을 적용했다. 본문의 180초 idle 예산은 헤더 수신 전에는 적용되지 않았다.
  같은 모델의 짧은 직접 요청은 약 25.88초에 성공했다.
- 수정: Ollama native 요청에만 초기 응답 예산 180초를 적용한다.
  외부 공급자 15초, 남은 전체 deadline, 취소 watcher, 응답 크기 제한은 유지한다.
  연결 자체의 별도 15초 제한을 보장하는 변경은 아니다.
- 회귀: 수정 전 native 예산 검사가 실패했고 수정 후 관련 133개가 통과했다.
  모델을 unload한 뒤 실제 서비스 경로의 grounded_basic 2건도 2/2 통과했다
  (첫 케이스 4.085초, 다음 1.340초). 이 재실행은 OS 캐시가 남은 상태이며,
  15초 초과 지연의 반복 재현이나 전체 출시 평가 통과를 의미하지 않는다.
- 진단 산출물은 `apps/api/artifacts/qa-evaluation/cold-load-fix-diagnostic/`에
  보존한다. 카테고리 제한·repeat=1 보고서는 출시 승인 근거로 사용하지 않는다.
- 코드 변경으로 `bf545b1`은 최종 검증 대상이 아니다. 새 커밋의 전체 평가와
  새 installer Windows 검증이 필요하며 출시 상태는 **HOLD**다.

## 1. 현재 상태

| 계층 | 내용 | 상태 |
|---|---|---|
| Layer 1 | 결정론적 파이프라인 평가(실제 서비스 경로) | ✅ 자동 검증 완료 |
| Layer 2 | 실제 Ollama + qwen3:4b/8b/14b 평가 | ⏳ 대기(수동 실행 필요) |
| Layer 3 | Windows 실기기 UX 검증 | ⏳ 대기 |

Layer 1은 실제 애플리케이션 경로(검색 → 컨텍스트 → provider → NDJSON 스트리밍 → 주장
검증 → 출처 재구성 → 저장)를 deterministic provider로 통과시켜, 러너·씨딩·독립 검증·집계·
게이트·보고서가 동작함을 확인했다. 의미 충실도(수치·단위·부정·비교 방향·상충)의 정오는
실제 모델이 있어야 판정할 수 있어 Layer 2에서 측정한다.

## 2. 자동 파이프라인 검증 결과 (Layer 1)

- 평가 프레임워크: `apps/api/app/qa_eval/` (manifest·synthetic·run_case·evaluate·metrics·
  gate·report·ollama·runner)
- 데이터셋: `apps/api/tests/fixtures/qa_evaluation/` (합성 문서 8종, 케이스 10건 이상)
- 테스트: `tests/unit/test_qa_eval_*.py`, `tests/integration/test_qa_eval_pipeline.py`
  - manifest 스키마·잘못된 케이스 거부
  - deterministic 실제 경로: grounded_basic 통과, 완전 부재 질문 not_found 보류
  - 독립 검증: 문서에 없는 수치·도입된 부정·타 문서 출처·금지 문구·인젝션 노출 →
    안전 위반으로 탐지
  - 집계: 안전 케이스 1회 실패 시 케이스 실패, 변동성(instability) 계산
  - 게이트: 안전 실패 → 출시 보류(4b는 allowlist 제외), 8b 기준 판정
  - 보고서: JSON/Markdown에 문서 원문·질문·모델 원문·API 키 미포함
- CI: 일반 CI(`uv run pytest -q`)가 위 Layer 1 테스트를 실행한다. **실제 모델을
  다운로드하지 않는다.** Layer 2는 opt-in 수동 실행이다.

취소·재시작 복구·revision 가드 mechanics는 Sprint 4B 테스트
(`tests/integration/test_qa_stream_api.py`)가 이미 CI에서 검증한다.

## 3. 실제 모델 평가 실행 방법 (Layer 2, 수동)

전제: Windows PC에 Ollama 설치·실행, 해당 모델 설치(앱 GUI로 다운로드).

```
# 균형형(기본 후보)
cd apps/api && uv run python scripts/evaluate_local_qa.py --model qwen3:8b --repeat 3 --fail-on-gate

# 경량형 / 고품질형 / 전문가형 비교
cd apps/api && uv run python scripts/evaluate_local_qa.py --model qwen3:4b --repeat 3
cd apps/api && uv run python scripts/evaluate_local_qa.py --model qwen3:14b --repeat 3
cd apps/api && uv run python scripts/evaluate_local_qa.py --model qwen3:30b-a3b --repeat 3
```

- 결과: `artifacts/qa-evaluation/qa-eval-qwen3-8b.json` / `.md` 등
- Ollama·모델 미설치 시 자동 다운로드 없이 skip
- 모델명은 allowlist(qwen3:4b/8b/14b/30b-a3b)만 허용, 127.0.0.1의 Ollama만 사용

## 4. 모델별 판정 (실제 평가 후 기록)

| 모델 | 판정 | 근거 |
|---|---|---|
| qwen3:8b | _대기_ | Layer 2 미실행 |
| qwen3:4b | _대기_ | Layer 2 미실행 |
| qwen3:14b | _대기_ | Layer 2 미실행 |
| qwen3:30b-a3b | _대기_ | Layer 2 미실행 |

판정 값: 기본 권장(default_recommended) / 선택 가능(selectable) / 경량 제한
(light_limited) / 출시 보류(release_hold) / allowlist 제외(allowlist_excluded).

## 5. 출시 게이트 강제 (fail-closed)

main 배포 워크플로(`.github/workflows/release.yml`)는 `scripts/check_release_gate.py`를
**필수 단계**로 실행한다. 승인 파일(`docs/testing/release-approval.json`)이 없거나, 판정이
미완이거나, **평가·검증한 코드 커밋(testedCommit) 이후 애플리케이션 코드가 바뀌었거나**,
평가 artifact 해시가 어긋나면 **비정상 종료해 발행을 차단**한다. 문서의 "HOLD"만으로는
막지 못하므로 워크플로 레벨에서 강제한다.

현재 승인 파일은 존재하지 않는다 → main 배포는 자동으로 차단된다(의도된 상태).

**자기참조 회피**: 승인 파일을 커밋하면 커밋 SHA가 바뀌므로 "승인.commit == 현재 SHA"는
정상 커밋으로 충족할 수 없다. 대신 승인은 실제로 평가·검증한 코드 커밋 `testedCommit`을
가리키고, 게이트는 (1) testedCommit이 현재 HEAD의 조상인지, (2) 그 이후 변경이 승인·보고·
artifact 파일로만 한정되는지(앱 코드·프롬프트·검색·출처 검증·평가기·release 워크플로가
바뀌면 거부), (3) artifact SHA-256 일치를 확인한다.

실제 평가·검증 완료 후 [출시 승인 artifact 형식](release-approval-format.md)에 따라 Qwen
평가 JSON과 Windows 실기기 검증 JSON을 작성한다. 게이트는 승인 문자열뿐 아니라 두 JSON의
`testedCommit`, `modelDigest`, `installerSha256`과 파일 SHA-256을 교차검증한다. 이후에는
승인·보고·artifact 파일만 커밋한다:

```json
{
  "testedCommit": "<평가·검증을 수행한 코드 커밋 전체 SHA>",
  "qwen3_8b": {
    "verdict": "default_recommended",
    "evalArtifact": "docs/testing/qa-eval-qwen3-8b.json",
    "modelDigest": "sha256:<64자리 hex>"
  },
  "windowsValidation": {
    "status": "passed",
    "validationArtifact": "docs/testing/windows-release-validation.json",
    "installerSha256": "<64자리 hex>"
  },
  "artifacts": [
    {"kind": "qwen3_8b_evaluation", "path": "...", "sha256": "<64자리 hex>"},
    {"kind": "windows_validation", "path": "...", "sha256": "<64자리 hex>"}
  ]
}
```

## 6. 출시 조건

- 공통 안전 게이트(사용자 표시 unsupported·타 문서 출처·인젝션 실행·수치/부정 조작 등
  전부 0)를 모든 후보 모델이 만족해야 해당 모델을 유지한다.
- qwen3:8b가 기본 모델 게이트(프로토콜 ≥95%, 상태 정확도 ≥90%, 유효 답변 ≥85%, 보류
  정확도 ≥95%, 핵심 의미 카테고리 100%)를 통과하고 Windows 실기기 검증이 통과해야
  기본 권장으로 출시한다.
- Windows 실기기 + 실제 qwen3:8b 평가 완료 전 main 배포/v0.1 정식 릴리스를 하지 않는다.
