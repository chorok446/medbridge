# Sprint 4C-A — 로컬 AI 온보딩 Windows 실기기 검증

로컬 AI(Ollama) 설치 안내·모델 다운로드·연결·요약/질문 사용을 실제 Windows PC에서
사용자가 직접 확인한다. AI는 GUI를 조작할 수 없어 이 단계를 대신할 수 없다.

## 자동 테스트 범위(요약)

- 백엔드 `tests/unit/test_local_ai_client.py` — version(정상/오래됨/미실행/redirect/과대응답/
  잘못된 JSON), tags allowlist 필터, test(성공/빈응답/thinking만/미허용 모델/문서 미전송),
  pull NDJSON 파싱·비JSON 건너뜀·미허용 거부, **외부 주소 호출 불가**.
- 백엔드 `tests/integration/test_local_ai_api.py` — status·models(설치여부·RAM/디스크 안내)·
  test(allowlist 422)·activate(로컬 저장·keyring 미저장·외부 덮어쓰기 409/확인)·
  pull(진행/완료·내부용어 미노출·미허용 422·미준비 409·중복 409).
- 백엔드 `tests/unit/test_local_provider_tuning.py` — 로컬 reasoning_effort=none·temperature 0·
  thinking 제거, 외부 provider 계약 유지, summary 로컬 키 불요.
- 프런트 `components/__tests__/local-ai-settings.test.tsx` — 설치 안내·다시 확인·추천/용량·
  RAM 경고·진행률(progressbar)·완료 후 연결 확인·기본 설정·외부 덮어쓰기 확인·기술정보 미노출.
- 프런트 `lib/api/__tests__/qa-stream.test.ts`(공용 NDJSON 파서) — UTF-8 경계·미완성 줄.

## 실기기 검증 항목 (Windows 10/11)

1. **관리자 권한 없이 설치 안내**: "설치 안내 열기"가 기본 브라우저로 공식 Ollama Windows
   페이지를 연다. MedBridge가 설치 파일을 대신 내려받지 않는다.
2. **미설치 상태 표시**: Ollama 미설치 시 "로컬 AI 실행 프로그램이 필요합니다" 안내.
3. **설치 후 다시 확인**: Ollama 설치·실행 후 "다시 확인" → 준비됨 화면으로 전환.
4. **qwen3:8b 다운로드**: 균형형(추천) "내려받기" 시작.
5. **다운로드 진행률**: 진행률 막대·백분율·현재 단계가 갱신(스크린리더로 진행률 읽힘).
6. **다운로드 중 앱 종료**: 창을 닫아도 오류 없이 종료된다(DB 잠김 없음).
7. **재실행 후 설치 상태 복구**: 다시 실행하면 실제 설치 상태를 다시 확인해 반영한다
   (완료됐으면 준비됨, 중단됐으면 다시 내려받기 가능).
8. **연결 테스트**: "연결 확인" → "로컬 AI를 사용할 준비가 됐습니다".
9. **요약 생성**: 문서 요약이 로컬 모델로 생성된다.
10. **Sprint 4B 스트리밍 질문**: 문서 Q&A가 로컬 모델로 점진 스트리밍된다.
11. **한글 답변·출처**: 한글 답변과 출처 배지(페이지 이동)가 정상.
12. **취소**: 스트리밍 질문 중 "중단"이 즉시 멈추고 다시 질문 가능.
13. **Ollama 종료 후 안전 오류**: Ollama를 끄고 질문/요약 시 안전한 안내(포트·stack trace 없음).
14. **다시 실행 후 자동 복구**: Ollama 재실행 후 정상 동작.
15. **오프라인에서 기존 모델 사용**: 인터넷 없이도 이미 설치된 모델로 요약·질문 가능.
16. **외부 AI 동의 없이 로컬 사용**: 외부 전송 동의 절차 없이 로컬 모델로 요약·질문.
17. **API 키 입력 없이 사용**: 로컬 AI 사용에 API 키 입력칸이 없다.
18. **4B 회귀 없음**: 취소·연결끊김 복구·문서 변경(revision) 가드가 그대로 동작.

## 보안 확인(실기기)

- 화면 어디에도 localhost/11434/endpoint/bearer/JSON/NDJSON/quantization/stack trace가 없다.
- 다운로드는 qwen3:4b/8b/14b/30b-a3b 외 모델을 받을 수 없다(allowlist).
- 연결 테스트에 문서 원문·질문·개인정보가 전송되지 않는다.
- 앱 제거 시 Ollama·모델이 자동 삭제되지 않는다. MedBridge가 `.ollama` 폴더를 만지지 않는다.

## 알려진 단순화

- loopback 단일 프로브로는 "설치 안 됨"과 "설치됐지만 실행 안 됨"을 구분할 수 없어
  둘을 `not_running`으로 통합한다. 설치 안내 + 다시 확인이 양쪽을 커버한다.
- 로컬/외부 설정은 단일 설정 행을 공유한다(별도 프로필 영속화는 범위 밖). 로컬 활성 시
  외부 설정을 덮어쓰기 전 확인창을 표시한다.
