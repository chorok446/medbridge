# 요약 공급자 네트워크 보안

`OpenAICompatibleSummaryProvider`가 사용자가 지정한 외부/로컬 모델 endpoint로
네트워크 요청을 보낼 때의 보안 정책. 구현: `app/services/summary/endpoint.py`,
`app/services/summary/provider.py`, `app/services/summary/settings_service.py`.

## 허용 endpoint 정책

| 모드 | scheme | 호스트 |
|---|---|---|
| 외부(`is_local=false`) | **HTTPS만** | 공인 IP로 해석되는 호스트만 |
| 로컬(`is_local=true`) | HTTP 또는 HTTPS | loopback만 (`localhost`, `127.0.0.0/8`, `::1`) |

입력 단계(`validate_endpoint`)에서 거부하는 것:
- scheme 누락 / http·https 이외(ftp/file/data/javascript/ws/wss)
- userinfo 포함 URL (`https://user:pass@host`)
- fragment·query 포함 URL
- CR/LF·제어문자, host 영역 percent-encoding
- 비정상 포트, 빈 hostname, 파싱 불가 URL

정규화: scheme·host(소문자)·port·path만 유지하고 trailing slash를 제거한다. 저장값과
실제 요청 URL이 항상 같도록 한다(query/fragment/userinfo는 저장 자체를 막는다).
model name은 URL path에 붙이지 않고 JSON body로만 보낸다(path injection 불가).

## 로컬 모델 정의

`is_local` 체크박스만 믿지 않는다. `is_local=true`여도 endpoint 호스트가 실제로
loopback(`localhost`/`127.0.0.0/8`/`::1`)일 때만 허용한다. LAN IP·사설 IP·`.local`·
VPN·Docker bridge·사용자 도메인은 로컬로 인정하지 않는다. 향후 LAN의 Ollama/LM Studio
지원이 필요하면 별도의 명시적 "로컬 네트워크 모델" 모드로 분리한다(현재는 loopback만
HTTP 허용).

## SSRF 방어 범위

요청 직전(`assert_host_allowed`) 호스트명을 해석해 모든 결과 IP를 정책 검증한다.
외부 모드에서 차단하는 대역: loopback, RFC1918(10/8·172.16/12·192.168/16), link-local
(169.254/16 — 클라우드 metadata 포함, fe80::/10), multicast(224/4), unspecified(0.0.0.0),
reserved, ULA(fc00::/7), IPv4-mapped IPv6 내부 주소, 그 밖에 `is_global`이 아닌 모든 IP.
`127.1`·`2130706433`·`0x7f000001` 같은 축약·정수 표기는 파이썬 `ipaddress`가 정규
IP로 해석한 뒤 위 정책으로 차단된다.

## redirect 정책

자동 redirect를 **추적하지 않는다**. 3xx 응답은 `redirect_blocked` 오류로 처리한다
(`_NoRedirect`). 연결 확인과 실제 요약 요청 모두 동일 정책을 쓴다. 이로써 redirect를
통한 내부 주소 우회와 API 키의 타 origin 유출을 원천 차단한다.

## API key 저장·전송

- 키는 OS keyring(Windows Credential Manager)에만 저장한다. SQLite DB·설정 JSON·
  로그·오류 보고서·HTTP 오류 메시지·프런트 응답 어디에도 평문으로 남지 않는다.
- 공개 설정 API는 `hasApiKey` 불리언만 반환한다(앞·뒤 일부도 표시하지 않음).
- 키는 요청을 만들 때만 메모리에서 읽어 `Authorization: Bearer` 헤더로 전송한다.
  query parameter로 전달하지 않는다. redirect를 막으므로 다른 origin으로 새지 않는다.
- 오류 객체(`SummaryNetworkError`)는 category만 담고 헤더·키·원문 오류를 담지 않는다.

## timeout·응답 크기 제한

`app/services/summary/settings.py`:
- 요약 요청 전체 timeout: `SUMMARY_REQUEST_TIMEOUT_SEC`(기본 60초)
- 연결 확인 timeout: `CONNECTION_TEST_TIMEOUT_SEC`(기본 10초)
- 정상·오류 응답 모두 `SUMMARY_MAX_RESPONSE_BYTES`(기본 4MB)까지만 streaming으로 읽는다.
  Content-Length가 상한을 넘으면 읽기 전에 거부하고, 없거나 거짓이어도 상한+1까지만
  읽어 초과를 감지한다. `Accept-Encoding: identity`로 압축을 요청하지 않아 압축 해제
  후 크기 폭증을 피한다.

> urllib은 단일 timeout만 지원해 connect/read를 분리하지 못한다. 전체 요청 timeout으로
> 근사한다.

## 로그 마스킹 정책

로그에는 진단에 필요한 최소 정보(provider type, 외부/로컬 여부, 오류 category,
correlation id)만 구조화해 남긴다. endpoint 전체 URL·query·Authorization/Cookie/
X-API-Key 등 민감 헤더·문서 청크 원문·프롬프트 원문·stack trace·API 키는 로그에
남기지 않는다.

## 남은 위험

- **DNS rebinding(TOCTOU)**: 요청 직전 해석한 IP를 검증하지만, 검증 후 실제 연결까지의
  짧은 창에서 DNS가 내부 IP로 바뀌는 rebinding은 완전히 제거하지 못한다. 완전한 DNS
  pinning(검증한 IP로 강제 연결 + TLS SNI)은 stdlib urllib 구조에서 과도하게 복잡해
  현재는 redirect 차단 + 요청 직전 재검증으로 흔한 SSRF 경로만 막는다. 개인용 로컬
  단일 사용자 데스크톱 앱이라 위험도는 낮다(공격자가 사용자 기기에서 악성 endpoint를
  직접 설정해야 성립).
- connect/read timeout 분리 불가(위 참조).

## 향후 LAN 모델 지원 시 고려사항

LAN의 Ollama/LM Studio를 지원하려면 loopback-only 제약을 완화해야 한다. 그때는 별도
"로컬 네트워크 모델" 모드를 두고, 사용자가 명시적으로 사설 대역 허용을 선택하게 하며,
전송 범위·경고를 강화한다. 기본값은 계속 loopback-only로 유지한다.
