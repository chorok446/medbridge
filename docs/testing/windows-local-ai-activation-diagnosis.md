# Windows 로컬 AI 활성화·대형 문서 요약 진단 기록 (2026-08-01)

> **출시 상태: HOLD**
> 이 기록은 Windows 실기기에서 확인한 사실과 현재까지 반영한 수정 범위를 고정한다.
> 대형 문서용 저장형 계층 요약과 Windows 36항목 재검증이 끝나기 전에는
> `release-approval.json`을 만들거나 출시 승인으로 전환하지 않는다.

## 1. 기준과 현재 상태

- 저장소: `chorok446/medbridge`
- 작업 브랜치: `fix/windows-local-ai-activation-save`
- 첫 수정 커밋: `aced2d3e30d0452ab6c92c8835a3a6414cf9c11a`
- Draft PR: <https://github.com/chorok446/medbridge/pull/6>
- 기존 출시 기준 커밋 `5cfb0a8c281d497429f1b826ba526dc60a52b2bf`는 코드 변경으로 무효다.
- `docs/testing/release-approval.json`: 생성하지 않음

GitHub Actions run `30687924315`에서 versions/backend/frontend/windows-build가 모두
성공했다. Windows artifact id는 `8814755645`, artifact SHA-256은 `51cf...f50f`,
설치 프로그램 SHA-256은 `46F1...783F`다. 해시는 이 진단 단계의 추적값이며 최종 출시
승인용 해시가 아니다.

## 2. 로컬 AI 활성화 저장 실패

### 재현과 확정 원인

- 사용자 DB는 약 2.018GB였고 삭제·초기화하지 않았다. 앱 종료 후 DB와 WAL/SHM을 함께
  백업하고 원본을 보존했다.
- DB 디렉터리 쓰기 가능, DB 파일 read-only 아님, `PRAGMA quick_check=ok`를 확인했다.
- 실제 장애는 추출 과정의 장시간 SQLite writer transaction과 약 530만 개
  `document_words`에 대한 누락된 FK 인덱스가 결합해 설정 저장 경합을 만든 것이다.
- Windows frozen sidecar의 고아 자식 프로세스가 DB를 계속 점유할 수 있는 종료 경로도
  함께 확인했다.
- 단순 권한 문제, `summary_settings` 중복 행, 이미 commit된 뒤 응답 직렬화만 실패한
  경우는 실기기 DB와 계측 결과로 배제했다.

### 반영한 수정 (`aced2d3`)

- SQLite busy timeout과 제한된 재시도, 안전한 실패 범주를 추가했다.
- migration `0009`에서 누락된 `document_words` FK 인덱스를 추가했다.
- 추출 준비 작업을 writer transaction 밖으로 이동하고 재시도 소진 시 반복을 멈춘다.
- 사용자 DB 백업은 SQLite online backup으로 수행한다.
- Windows frozen sidecar를 Job Object로 묶고 자기 프로세스 join을 피하도록 종료
  경계를 보강했다.
- 프런트는 중복 활성화 클릭을 막고, 성공 후 summary settings를 무효화해 다시 조회한다.

### 실기기 결과

- migration `0009` 적용 성공
- qwen3:8b 연결 확인 성공
- 기본 모델 활성화 성공, 화면 메시지: `이 모델을 기본 AI로 설정했어요.`
- DB에는 활성 summary setting이 정확히 1개이며 provider는 `openai_compatible`,
  endpoint는 `http://127.0.0.1:11434/v1`, model은 `qwen3:8b`, `is_local=1`로 확인됐다.
- 대형 문서 청크 재생성은 7,965 chunks까지 성공했다.

## 3. 대형 문서 요약에서 새로 확인한 실패

### 관찰값

- 실기기 문서: 1,104 pages, 7,965 chunks, 4,265,890 extracted characters
- 기존 section-title 우선 grouping 결과: 3,074 map groups
- 현재 파이프라인이 끝까지 실행되면 map 3,074회와 최종 reduce 1회가 순차 실행된다.
- UI는 약 150.82초 후 `요약을 만들지 못했어요. 다시 시도해 주세요.`를 표시했다.
- DB의 최신 run은 `FAILED / SUMMARY_FAILED`, artifact는 0개였다.
- 안전 오류 보고서: `C:\tmp\medbridge-summary-failure-aced2d3.zip`
  (802 bytes, SHA-256
  `0C7CDAB1956F7D4693F93049B28880C759264E9520F1528533481C17D01F7833`)

### 원인 계측

문서·프롬프트·응답 원문이나 토큰을 출력하지 않는 로컬 probe로 확인했다.

- 첫 20개 map group: 모두 성공, 합계 44.672초
- 다음 구간: group index 70에서 재현
- 응답: `finish_reason=length`, `completion_tokens=2048`(설정 상한과 동일)
- 결과 JSON: 닫히지 않은 문자열로 파싱 실패
- 동일 입력에 `summary` 한 필드, 3문장/400자, `max_tokens=512` 계약을 사용한 probe:
  2.765초, `finish_reason=stop`, 127 completion tokens, 유효 JSON, 164자 요약

따라서 즉시 실패의 확정 원인은 **모델 출력이 토큰 상한에서 잘린 불완전 JSON**이다.
이와 별개로 3,075회의 순차 호출은 300MB급 문서를 실용적으로 처리하기 어려운 구조적
확장성 문제다.

## 4. 이 문서와 함께 커밋하는 두 번째 수정

- map 출력 계약을 `{"summary":"..."}` 중심의 3문장/400자 이하로 축소하고
  `max_tokens=512`를 적용한다.
- `finish_reason`이 `stop`이 아니거나 JSON/필드/길이 계약이 틀리면 저장하지 않고
  `invalid_response`로 안전하게 분류한다.
- 모델에 실제 chunk UUID를 보내지 않는다. map 출처는 요청에 포함된 chunk를 서버가
  소유하고, reduce는 짧은 group token만 받아 서버가 실제 chunk ID로 환원한다.
- 임의 타입 강제 변환과 조용한 문자열 자르기를 제거하고, 출처 없는 artifact는 계속
  폐기한다.
- 모델이 만든 `studyCautions`는 문서 근거처럼 저장하지 않는다. 안전 고지는 UI의
  시스템 소유 고정 배너로 유지한다.
- structlog를 표준 logging handler로 라우팅하고 embedded Alembic이 handler를
  덮어쓰지 않게 해 `sidecar.log`에 안전한 실패 범주가 남도록 한다.
- UI는 `timeout`과 `invalid_response`에 안전한 한국어 안내를 제공하고, 최신 재시도가
  실패해도 이전 성공 요약을 유지한다.

이 수정은 확인된 JSON 잘림을 해결하지만, 3,074개 group의 전체 처리 시간과 중단 후
재개 문제까지 해결한 것은 아니다.

## 5. 자동 검증

- Backend full pytest: `538 passed, 3 skipped`
- Backend focused summary/migration tests: `49 passed`
- Ruff (변경된 backend 파일): 통과
- Frontend Vitest: `17 files, 97 tests` 통과
- Frontend TypeScript `tsc --noEmit`: 통과
- Frontend 변경 파일 ESLint: 통과
- `git diff --check`: 통과

pytest에는 기존 Starlette deprecation warning과 Windows cp949 reader-thread warning이
각 1건 남지만 테스트 실패는 없었다.

## 6. 300MB 이상 문서 지원 방향

문서 크기나 group 수로 사용을 막는 방식은 채택하지 않는다. 다음 구현은 입력 파일
크기가 아니라 추출된 텍스트량을 기준으로 동작해야 한다.

1. section 제목이 바뀔 때마다 group을 끊지 않고 reading order를 보존한 bounded
   char/token packing을 공통 함수로 사용한다.
2. main DB에 run별 summary node/checkpoint를 저장한다.
3. 각 node는 서버 소유 source IDs, 입력 hash, 출력 hash, 상태와 시도 횟수를 보관한다.
4. bounded fan-in으로 여러 단계 reduce를 수행해 어떤 단계도 모델 context를 넘지 않는다.
5. 앱 종료·재실행 시 revision/chunk hash/model/prompt가 같으면 성공 node를 재사용하고,
   달라졌으면 섞지 않고 새 run으로 시작한다.
6. 각 호출 전후 취소와 revision을 확인하고, 처리 chunk 수와 모델 호출 진행률을 UI에
   제공한다.
7. 빠른 대표 개요와 전체 정밀 요약을 구분해 대표 근거만 사용한 결과를 전체 내용의
   완전한 요약으로 표시하지 않는다.

Ollama의 OpenAI-compatible endpoint에서는 request별 context size 설정을 지원하지
않으므로, 향후 로컬 전용 최적화는 native `/api/chat`의 `options.num_ctx`, JSON schema
structured output, `think=false`, `keep_alive`를 별도 검증한 뒤 적용한다.

- OpenAI compatibility: <https://docs.ollama.com/api/openai-compatibility>
- Native chat API: <https://docs.ollama.com/api/chat>
- Structured outputs: <https://docs.ollama.com/capabilities/structured-outputs>

## 7. 데이터 보호와 남은 출시 게이트

- 사용자 DB/PDF/설정 행은 삭제하거나 초기화하지 않았다.
- 진단 출력에는 API token, 문서 원문, 질문·응답 원문을 남기지 않았다.
- 합성 smoke PDF와 개발 중 patch 파일은 커밋 대상이 아니다.
- 새 요약 pipeline 구현 후 새 커밋으로 CI와 Windows installer를 다시 만들어야 한다.
- 새 설치본은 Windows 실기기에서 활성화 유지, 소형 문서 요약/Q&A 성공, 대형 문서
  진행·취소·재시작 재개를 확인해야 한다.
- PR 병합 후 새 develop merge commit으로 qwen3:8b repeat 3 평가와 Windows 36항목을
  처음부터 다시 수행한다.
- 위 조건을 모두 통과하기 전 최종 판정은 계속 **HOLD**다.
