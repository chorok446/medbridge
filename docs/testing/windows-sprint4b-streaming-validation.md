# Sprint 4B — Q&A 스트리밍 검증

검증된 출처 기반 Q&A 스트리밍(NDJSON)의 자동 테스트 범위와 Windows 실기기 확인 항목.

## 자동 테스트

### 백엔드 (`apps/api`)

- `tests/unit/test_qa_streaming.py`
  - NDJSON 프로토콜: `encode_event`가 줄 내부 개행을 이스케이프해 한 이벤트=한 줄 보장.
  - `public_source_ref`가 내부 chunk id를 제거하고 UI 이동 필드만 남김.
  - `DeterministicStreamingQaProvider`: claim→final 순서, 빈 청크 → `not_found`, 즉시 취소 시 claim 미방출.
  - `OpenAICompatibleStreamingQaProvider`: SSE delta가 토큰 단위로 쪼개져 도착해도 NDJSON 한 줄로
    재조립, `[DONE]` 종료, 비-semantic 줄 무시(네트워크 없이 `line_source` 주입).
- `tests/integration/test_qa_stream_api.py`
  - happy path: `started`→`phase`→`claim`(출처 포함)→`completed`, 응답 `content-type`은 `application/x-ndjson`.
  - 출처 안전: 검색되지 않은 chunk id를 붙인 claim은 스트림으로 나가지 않고 최종 상태는
    `insufficient_evidence`. 이벤트 어디에도 `chunkId` 미포함.
  - 동시성: 진행 중(streaming) assistant가 있으면 새 스트림 시작은 409(부분 유니크 인덱스).
  - 취소: 스트림 도중 취소 요청 → `cancelled` 이벤트 방출 + DB 상태 `cancelled`, 이후 같은 스레드에서
    새 스트림 시작 가능.
  - 취소 멱등: 이미 완료된 메시지에 cancel → 200(현재 상태 반환).
  - 상태 조회: 완료 후 status=`completed`, 다른 스레드의 메시지 id는 404.
  - revision 가드: 저장 직전 청크 해시 변경 감지 → `interrupted`(REVISION_CHANGED), `completed` 없음,
    주장 0건 저장, user 질문은 보존.
  - 동의 게이트: 외부 공급자에 문서 동의 없음 → 스트림 시작 전 403.
  - 재시작 복구: pending/streaming/finalizing 상태로 남은 스트림은 재시작 시 `interrupted`로 정리.

### 프런트 (`apps/web`)

- `lib/api/__tests__/qa-stream.test.ts`
  - NDJSON 파서: 한 청크의 여러 이벤트 분리, 여러 청크에 걸친 한 줄 재조립,
    UTF-8 멀티바이트가 청크 경계에서 쪼개져도 안전, 개행 없이 끝난 마지막 줄 처리(end),
    깨진 JSON 줄은 조용히 폐기.
- `components/__tests__/document-qa.test.tsx`
  - 스트리밍 진행 중 검증된 주장을 점진적으로 표시, 스트림 종료 후 확정 답변·출처 표시.
  - 중단 버튼 → 취소 API 호출.
  - 501/403 안내, 출처 클릭 이동, 기술 정보(chunk id·모델명·bbox 숫자) 미노출.

### 마이그레이션

- `0007 ↔ 0008` upgrade/downgrade 왕복 확인(스트림 컬럼 + 활성 assistant 부분 유니크 인덱스
  `status IN ('pending','streaming','finalizing')` 재생성).

## 실기기 검증 항목 (Windows / WebView2)

AI가 GUI를 조작할 수 없어 실제 Windows PC에서 사용자가 직접 확인한다.

1. **점진 표시**: 질문 후 근거가 확인되는 대로 주장이 하나씩 나타나고, 아래에
   단계 안내("근거를 찾고 있어요…" → "답변을 작성하고 있어요…" → "마무리하고 있어요…")가 갱신된다.
2. **출처 배지**: 스트리밍 중 나타난 각 주장의 "N쪽" 배지를 누르면 PDF가 해당 페이지로 이동하고
   하이라이트가 깜빡인다.
3. **중단**: 진행 중 "중단"을 누르면 즉시 멈추고, 같은 대화에서 바로 새 질문을 보낼 수 있다.
4. **연결 끊김**: 스트리밍 중 앱을 강제 종료 후 재실행하면 이전 답변이 "연결이 끊겨 완료하지 못했어요"로
   정리되고 "다시 시도"가 뜬다(진행 중 상태로 멈춰 있지 않음).
5. **문서 변경 중 질문**: 스트리밍 중 문서를 재처리(재청크)하면 답변이 저장되지 않고
   "문서 내용이 변경되어 답변을 다시 만들어야 합니다" 안내가 나온다.
6. **기술 정보 비노출**: 화면 어디에도 포트·토큰·chunk id·모델명·bbox 숫자가 보이지 않는다.
7. **외부 모델 동의**: 외부 모델 설정 시 문서 외부 전송 동의 전에는 스트림이 시작되지 않고 안내가 뜬다.
8. **WebView2 안정성**: 긴 답변(수십 개 주장)에서도 스크롤·렌더가 끊기지 않고 메모리가 계속 증가하지 않는다.
