"""요약 모델 endpoint 검증 + SSRF 방어 + 안전한 HTTP POST (stdlib만 사용).

정책 요약:
- 외부 모델: HTTPS만, 공인 IP로만 연결.
- 로컬 모델: HTTP/HTTPS, loopback 호스트(localhost/127.0.0.0/8/::1)로만.
- redirect는 추적하지 않는다(3xx는 오류) — 내부 주소 우회·API 키 유출 방지.
- 응답은 크기 상한까지만 streaming으로 읽는다.

남은 위험: 완전한 DNS pinning(검증한 IP로 강제 연결 + TLS SNI)은 stdlib urllib
구조에서 과도하게 복잡해, 요청 직전 해석한 IP를 정책 검증하는 방식(TOCTOU 창 존재)을
쓴다. redirect 차단 + 요청 직전 재검증으로 흔한 SSRF 경로는 막지만, 검증 후 DNS가
내부 IP로 바뀌는 rebinding은 완전히 제거하지 못한다. 자세한 내용은
docs/security/summary-provider-network-security.md 참조.
"""

import codecs
import ipaddress
import json as _json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

# 안전 로컬 호스트명 (정확히 일치해야 함 — localhost.evil.example 등은 제외)
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

# 컨텍스트 초과 표지는 **경로마다 다르다**. 하나로 합치면 한쪽에서 오분류가 난다.
#
# (1) HTTP 400 본문: 서버가 기계적으로 만든 문구다. 컨텍스트를 명시적으로 가리키는
#     것만 인정한다. "too long"·"too large" 같은 일반 문구는 컨텍스트와 무관한 400에도
#     흔해서(예: string_above_max_length, Request payload too large), 그것까지 초과로
#     보면 외부 유료 API를 분할 재시도로 20배 낭비하고 "메모리를 확보하라"는 무의미한
#     안내를 띄운다.
HTTP_CONTEXT_OVERFLOW_HINTS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "exceeds the context",
    "maximum context",
    "available context",
)

# (2) 모델이 HTTP 200으로 돌려준 error 객체: Ollama는 프롬프트를 조용히 자르므로 유일한
#     단서가 모델의 자연어 문구다. 실측된 영어 표현에 더해, 한국어로 답하도록 지시된
#     모델이 내는 한국어 표현도 함께 본다 — 영어만 보면 한국어 응답에서 적응 분할이
#     한 번도 발동하지 않는다.
#     맨 "context"는 넣지 않는다("I cannot summarize this without more context").
MODEL_CONTEXT_OVERFLOW_HINTS = (
    *HTTP_CONTEXT_OVERFLOW_HINTS,
    "too long",
    "too large",
    "excessive repetition",
    "너무 깁니다",
    "너무 길어",
    "너무 많습니다",
    "입력이 길",
    # 단독 "컨텍스트"는 넣지 않는다 — 맨 "context"를 뺀 것과 같은 이유다.
    # "요약하려면 더 많은 컨텍스트가 필요합니다"는 초과가 아니라 그 반대인데,
    # 초과로 오판하면 그룹을 깊이 3까지 쪼개 20회 넘는 호출을 태우고 끝내
    # "메모리를 확보하라"는 실행 불가 안내가 뜬다.
    "컨텍스트 길이",
    "컨텍스트 창",
    "컨텍스트를 초과",
    "컨텍스트 초과",
    "최대 컨텍스트",
)

# HTTP 오류 본문에서 초과 여부만 판별하려고 읽는 최대 바이트. 본문은 분류에만 쓰고
# 로그·사용자 메시지에는 절대 남기지 않는다(모델이 원문을 되비출 수 있다).
_ERROR_BODY_SNIFF_BYTES = 8 * 1024

# 오류 범주 — 사용자에게는 이 범주에 대응하는 안전한 메시지만 보여준다.
NET_ERROR_MESSAGES = {
    "invalid_address": "주소가 올바르지 않습니다.",
    "unsafe_address": "안전하지 않은 주소입니다.",
    "connect_failed": "모델 서비스에 연결할 수 없습니다.",
    "auth_failed": "인증에 실패했습니다. API 키를 확인해 주세요.",
    "forbidden": "접근 권한이 없습니다.",
    "rate_limited": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    "model_not_found": "모델을 찾을 수 없습니다. 모델 이름을 확인해 주세요.",
    "server_error": "모델 서비스에서 오류가 발생했습니다.",
    "bad_response": "모델 응답 형식이 올바르지 않습니다.",
    # 서버가 요청마다 num_ctx를 지정하므로 "컨텍스트를 늘리라"고 안내하면 사용자가
    # 설정을 바꿔도 아무 효과가 없다. 실제로 유효한 조치만 안내한다.
    "context_overflow": (
        "문서 한 조각이 이 모델이 한 번에 볼 수 있는 크기를 넘었습니다. "
        "메모리가 더 넉넉한 상태에서 다시 시도하거나 더 작은 모델을 사용해 주세요."
    ),
    "response_too_large": "모델 응답이 너무 큽니다.",
    "redirect_blocked": "안전하지 않은 리디렉션이 차단되었습니다.",
    "timeout": "모델 응답 시간이 초과되었습니다.",
}


class SummaryNetworkError(Exception):
    """요약 네트워크 오류 — category만 노출하고 원문 오류·비밀은 담지 않는다.

    `reason`은 같은 category 안에서 **어느 계약이 깨졌는지**를 가리키는 짧은 분류값이다.
    `bad_response` 하나에 HTTP 400·JSON 파싱 실패·finish_reason 절단·필드 타입 위반·
    길이 초과·group id 불일치가 전부 뭉쳐 있어 로그만으로는 원인을 좁힐 수 없었다.
    reason에는 문서·질문·모델 출력 원문을 절대 담지 않는다(분류값·수치만).
    """

    def __init__(
        self,
        category: str,
        reason: str | None = None,
        *,
        oversized_text: str | None = None,
    ) -> None:
        super().__init__(category if reason is None else f"{category}:{reason}")
        self.category = category
        self.reason = reason
        # 계약 길이를 넘겨서 거절된 모델 출력. 실행기가 마지막 수단으로 잘라 쓰기 위한
        # 값이라 예외에 들고 다닌다 — 문서 전체 요약을 잃는 것보다 한 그룹이 잘리는
        # 편이 낫다. **로그·사용자 메시지에는 절대 넣지 않는다**(reason과 달리 원문이다).
        self.oversized_text = oversized_text

    @property
    def user_message(self) -> str:
        return NET_ERROR_MESSAGES.get(self.category, "모델 서비스 오류가 발생했습니다.")


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def is_loopback_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h in _LOCAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def _ip_is_blocked_for_external(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """외부 endpoint가 연결하면 안 되는 IP인지 — 내부·특수 대역 전부 차단."""
    # IPv4-mapped IPv6(::ffff:a.b.c.d)는 내부 IPv4로 매핑되므로 원래 IPv4로 판정
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 (클라우드 metadata 포함), fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def validate_endpoint(raw: str | None, *, is_local: bool) -> str:
    """endpoint를 엄격히 파싱·정규화해 반환한다. 위반 시 SummaryNetworkError.

    정규화: scheme·host(소문자)·port·path만 유지, trailing slash 제거. query/fragment/
    userinfo/params는 거부한다(저장값과 실제 요청값이 달라지지 않게).
    """
    if raw is None or not raw.strip():
        raise SummaryNetworkError("invalid_address")
    value = raw.strip()
    if _has_control_chars(value):  # CR/LF·제어문자
        raise SummaryNetworkError("invalid_address")

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise SummaryNetworkError("invalid_address") from exc

    if parts.scheme not in ("http", "https"):
        raise SummaryNetworkError("invalid_address")
    if parts.username or parts.password or "@" in parts.netloc:  # userinfo 금지
        raise SummaryNetworkError("invalid_address")
    if parts.fragment or parts.query:  # fragment·query 금지
        raise SummaryNetworkError("invalid_address")
    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise SummaryNetworkError("invalid_address") from exc
    if not hostname:
        raise SummaryNetworkError("invalid_address")
    # host 영역의 percent-encoding·제어문자로 검증을 우회하지 못하게 한다
    if _has_control_chars(hostname) or "%" in parts.netloc:
        raise SummaryNetworkError("invalid_address")
    try:
        port = parts.port  # 비정상 포트면 ValueError
    except ValueError as exc:
        raise SummaryNetworkError("invalid_address") from exc
    if port is not None and not (1 <= port <= 65535):
        raise SummaryNetworkError("invalid_address")

    if is_local:
        # 로컬: http/https 모두 허용하되 loopback 호스트만
        if not is_loopback_hostname(hostname):
            raise SummaryNetworkError("unsafe_address")
    else:
        # 외부: HTTPS만. 명백한 loopback 호스트명은 즉시 거부(해석은 요청 시 재검증).
        if parts.scheme != "https":
            raise SummaryNetworkError("unsafe_address")
        if is_loopback_hostname(hostname):
            raise SummaryNetworkError("unsafe_address")

    host_norm = hostname.lower()
    if ":" in host_norm:  # IPv6 리터럴은 대괄호로 다시 감싼다
        host_norm = f"[{host_norm}]"
    netloc = f"{host_norm}:{port}" if port is not None else host_norm
    path = (parts.path or "").rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def _resolve(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(hostname, port or None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SummaryNetworkError("connect_failed") from exc
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    if not ips:
        raise SummaryNetworkError("connect_failed")
    return ips


def assert_host_allowed(hostname: str, port: int, *, is_local: bool) -> None:
    """요청 직전 호스트명을 해석해 정책을 재검증한다(SSRF 방어의 핵심)."""
    ips = _resolve(hostname, port)
    if is_local:
        if not all(ip.is_loopback for ip in ips):
            raise SummaryNetworkError("unsafe_address")
    else:
        if any(_ip_is_blocked_for_external(ip) for ip in ips):
            raise SummaryNetworkError("unsafe_address")


def _assert_url_allowed(url: str, *, is_local: bool) -> None:
    """요청 직전 URL의 호스트를 해석해 정책을 재검증한다(get/post/stream 공통)."""
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    assert_host_allowed(hostname, port, is_local=is_local)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx redirect를 추적하지 않는다 — 내부 주소 우회·API 키 유출 방지."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise SummaryNetworkError("redirect_blocked")


def _auth_headers(api_key: str, accept: str) -> dict[str, str]:
    """공통 요청 헤더. api_key가 비어 있으면 Authorization을 넣지 않는다
    (로컬 Ollama는 키가 없으므로 'Bearer None' 같은 가짜 값을 보내지 않는다)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        # 압축 해제 후 크기 폭증을 피하려고 압축을 요청하지 않는다
        "Accept-Encoding": "identity",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def get_json(
    url: str,
    *,
    is_local: bool,
    timeout: float,
    max_response_bytes: int,
) -> dict:
    """검증된 endpoint로 GET(JSON). post_json과 동일한 안전 정책(정책 재검증·redirect
    차단·크기/데드라인 상한). 인증 헤더는 보내지 않는다(Ollama 로컬 조회용)."""
    _assert_url_allowed(url, is_local=is_local)

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    raw = _read_capped(opener, req, timeout=timeout, max_response_bytes=max_response_bytes)
    try:
        return _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SummaryNetworkError("bad_response") from exc


def _read_capped(
    opener: urllib.request.OpenerDirector,
    req: urllib.request.Request,
    *,
    timeout: float,
    max_response_bytes: int,
) -> bytearray:
    """열기·크기/데드라인 상한 읽기·오류 범주화를 공유한다(post_json/get_json)."""
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 (검증된 endpoint)
            length = resp.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_response_bytes:
                        raise SummaryNetworkError("response_too_large")
                except ValueError:
                    pass  # 거짓 Content-Length는 무시하고 streaming 상한으로 막는다
            deadline = time.monotonic() + timeout
            raw = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise SummaryNetworkError("timeout")
                piece = resp.read(65536)
                if not piece:
                    break
                raw.extend(piece)
                if len(raw) > max_response_bytes:
                    raise SummaryNetworkError("response_too_large")
    except SummaryNetworkError:
        raise
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from None
    except TimeoutError as exc:
        raise SummaryNetworkError("timeout") from exc
    except OSError as exc:
        if isinstance(exc, socket.timeout):
            raise SummaryNetworkError("timeout") from exc
        raise SummaryNetworkError("connect_failed") from exc
    if len(raw) > max_response_bytes:
        raise SummaryNetworkError("response_too_large")
    return raw


def post_json(
    url: str,
    payload: dict,
    api_key: str,
    *,
    is_local: bool,
    timeout: float,
    max_response_bytes: int,
) -> dict:
    """검증된 endpoint로 JSON POST. redirect 차단·크기 제한·정책 재검증을 적용한다.

    api_key는 Authorization 헤더로만 전송하고, 오류에는 담지 않는다.
    """
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # 요청 직전 정책 재검증 (저장 시점과 DNS가 달라졌을 수 있다)
    assert_host_allowed(hostname, port, is_local=is_local)

    body = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=_auth_headers(api_key, "application/json"),
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    raw = _read_capped(opener, req, timeout=timeout, max_response_bytes=max_response_bytes)
    try:
        return _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SummaryNetworkError("bad_response") from exc


# finish_reason 값을 로그에 남길 때 모델이 준 임의 문자열을 그대로 쓰지 않는다.
KNOWN_FINISH_REASONS = frozenset({"length", "content_filter", "tool_calls", "function_call"})


def parse_chat_content(data: dict) -> str:
    """OpenAI 호환 chat/completions envelope에서 완결된 텍스트 content를 꺼낸다.

    length/content_filter 등 stop이 아닌 finish_reason은 완결된 JSON 계약이 아니므로
    파싱 전에 명시적으로 거부한다. 요약·Q&A 두 파서가 이 한 곳을 공유한다 —
    갈라지면 같은 절단 응답이 한쪽에서만 원인 불명(bad_response)으로 남는다.
    """
    from app.services.model_output import strip_thinking

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryNetworkError("bad_response", "envelope_shape") from exc
    finish_reason = choice.get("finish_reason")
    if finish_reason not in (None, "stop"):
        # 토큰 상한 절단(length)인지 다른 중단인지 구분해 둔다 — 대응이 다르다.
        safe = finish_reason if finish_reason in KNOWN_FINISH_REASONS else "other"
        raise SummaryNetworkError("bad_response", f"finish_{safe}")
    if not isinstance(content, str):
        raise SummaryNetworkError("bad_response", "content_not_text")
    # thinking 흔적은 UI·저장·로그에 남기지 않는다(JSON 파싱 전에 제거).
    return strip_thinking(content)


def stream_lines(
    url: str,
    payload: dict,
    api_key: str,
    *,
    is_local: bool,
    connect_timeout: float,
    idle_timeout: float,
    total_deadline: float,
    max_line_bytes: int,
    max_total_bytes: int,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """검증된 endpoint로 JSON POST 후 응답 본문을 UTF-8 줄 단위로 스트리밍한다.

    post_json과 동일한 안전 정책(HTTPS/loopback·DNS 재검증·redirect 차단·API 키 헤더·
    크기 상한)을 적용하되, 취소 가능하고 idle/total deadline을 강제한다. should_cancel()이
    True면 즉시 응답을 닫고 종료한다. UTF-8 멀티바이트가 청크 경계에서 나뉘어도 안전하다.
    """
    _assert_url_allowed(url, is_local=is_local)

    body = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=_auth_headers(api_key, "text/event-stream"),
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    resp = None
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    total = 0
    deadline = time.monotonic() + total_deadline
    try:
        try:
            resp = opener.open(req, timeout=connect_timeout)  # noqa: S310 (검증된 endpoint)
        except SummaryNetworkError:
            raise
        except urllib.error.HTTPError as exc:
            raise _classify_http_status(exc.code) from None
        except TimeoutError as exc:
            raise SummaryNetworkError("timeout") from exc
        except OSError as exc:
            raise SummaryNetworkError("connect_failed") from exc

        # 개별 읽기는 idle_timeout, 전체는 total_deadline으로 제한
        try:
            resp.fp.raw._sock.settimeout(idle_timeout)  # type: ignore[attr-defined]
        except Exception:
            pass

        while True:
            if should_cancel is not None and should_cancel():
                return
            if time.monotonic() > deadline:
                raise SummaryNetworkError("timeout")
            try:
                chunk = resp.read(8192)
            except TimeoutError as exc:
                raise SummaryNetworkError("timeout") from exc
            except OSError as exc:
                raise SummaryNetworkError("connect_failed") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_total_bytes:
                raise SummaryNetworkError("response_too_large")
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if len(line.encode("utf-8")) > max_line_bytes:
                    raise SummaryNetworkError("response_too_large")
                yield line
                if should_cancel is not None and should_cancel():
                    return
        # 남은 미완결 버퍼는 폐기(불완전 줄)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _classify_http_error(exc: urllib.error.HTTPError) -> SummaryNetworkError:
    """HTTP 오류 → 범주. 400은 본문을 훑어 컨텍스트 초과인지 먼저 가른다.

    OpenAI 호환 서버(LM Studio·llama.cpp server·vLLM·OpenAI)가 컨텍스트 초과를 알리는
    유일한 명시적 신호는 400 + `context_length_exceeded` 본문이다. 이걸 bad_response로
    뭉뚱그리면 executor의 적응 분할이 발동하지 않아 문서 전체가 즉시 실패한다.
    본문은 이 판별에만 쓰고 어디에도 저장하지 않는다.
    """
    if exc.code == 400:
        try:
            body = exc.read(_ERROR_BODY_SNIFF_BYTES).decode("utf-8", "replace").lower()
        except Exception:  # noqa: BLE001 — 본문을 못 읽으면 코드만으로 분류한다
            body = ""
        if any(hint in body for hint in HTTP_CONTEXT_OVERFLOW_HINTS):
            return SummaryNetworkError("context_overflow", "http_400_context_length")
    return _classify_http_status(exc.code)


def _classify_http_status(code: int) -> SummaryNetworkError:
    if code in (401,):
        return SummaryNetworkError("auth_failed", f"http_{code}")
    if code in (403,):
        return SummaryNetworkError("forbidden", f"http_{code}")
    if code in (404,):
        return SummaryNetworkError("model_not_found", f"http_{code}")
    if code == 429:
        return SummaryNetworkError("rate_limited", f"http_{code}")
    if 500 <= code <= 599:
        return SummaryNetworkError("server_error", f"http_{code}")
    # 400(잘못된 파라미터 등)도 여기로 온다 — 응답 내용 위반과 구분되도록 reason을 남긴다.
    return SummaryNetworkError("bad_response", f"http_{code}")
