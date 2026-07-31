# 요약 공급자 보안 검증

## 자동 테스트

- `apps/api/tests/unit/test_summary_endpoint.py`
  - endpoint 검증: 외부 HTTPS 허용 / 외부 HTTP 거부 / 로컬 localhost·127.0.0.1·::1 허용 /
    file·data·javascript·ftp·ws 거부 / userinfo·CRLF·fragment·query·비정상 포트 거부 /
    `localhost.evil.example`·`127.0.0.1.evil.example` 외부로 판정 / 정규화(trailing slash·
    host 소문자).
  - IP 정책: loopback·RFC1918·link-local·multicast·unspecified·ULA·IPv4-mapped 내부 차단,
    공인 IP 허용.
  - SSRF: 외부 hostname이 내부 IP로 해석되면 차단 / 공인 IP면 허용 / 로컬 모드에서
    비-loopback 해석 차단 (`socket.getaddrinfo` monkeypatch).
  - 안전 POST(127.0.0.1 loopback 서버): 정상 소형 JSON 성공 / 302 redirect 차단 /
    4MB 초과 응답 거부 / 비-JSON 거부 / timeout / 401·403·404·429·5xx 범주화 /
    오류에 API 키 미포함.
- `apps/api/tests/integration/test_summary_settings.py`
  - 라우트 endpoint 검증: 외부 HTTP·userinfo·로컬 비-loopback 422, 로컬 loopback 정규화 저장.
  - 비밀 미노출: `MEDBRIDGE_TEST_SECRET_DO_NOT_LEAK`가 설정 응답·연결 확인·오류 보고서·
    DB 어디에도 등장하지 않음.
  - 설정 원자성: keyring 저장 실패 시 DB 롤백(부분 저장 방지).
  - 연결 확인 오류 범주화 메시지.
- `apps/api/tests/integration/test_summary_api.py`
  - 외부 공급자 동의 게이트(동의 없음 403 / 동의 있음 202), 기능 회귀(Disabled·검색·
    이전 성공 요약 유지 등).

## 실기기 검증 항목

1. 로컬 endpoint(Ollama 등 `http://127.0.0.1:...`) 연결 확인 성공.
2. 외부 HTTPS endpoint 연결 확인 성공.
3. 외부 HTTP endpoint 저장 거부(안전하지 않은 주소 안내).
4. 잘못된 API 키 → "인증에 실패했습니다" 범주 메시지.
5. 응답 지연 → "응답 시간이 초과되었습니다".
6. 대용량 응답 → "응답이 너무 큽니다".
7. redirect 응답 → 차단 안내.
8. 앱 재실행 후에도 키 유지(keyring).
9. 설정에서 키 삭제 후 `hasApiKey=false`, keyring 값 제거 확인.
10. sidecar.log·오류 보고서 ZIP에 API 키·endpoint query·문서 청크 원문이 없는지 점검.
