"""요약 endpoint 검증·SSRF 정책·안전 HTTP POST 단위 테스트.

네트워크 관련 테스트는 127.0.0.1 loopback HTTP 서버만 사용한다(외부 접속 없음).
"""

import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.summary.endpoint import (
    SummaryNetworkError,
    _ip_is_blocked_for_external,
    assert_host_allowed,
    post_json,
    validate_endpoint,
)


class TestValidateEndpoint:
    def test_external_https_allowed(self):
        assert validate_endpoint("https://api.example.com/v1", is_local=False) == (
            "https://api.example.com/v1"
        )

    def test_external_http_rejected(self):
        with pytest.raises(SummaryNetworkError) as e:
            validate_endpoint("http://api.example.com/v1", is_local=False)
        assert e.value.category == "unsafe_address"

    def test_local_http_localhost_allowed(self):
        assert validate_endpoint("http://localhost:11434/v1", is_local=True) == (
            "http://localhost:11434/v1"
        )

    def test_local_http_127_allowed(self):
        assert validate_endpoint("http://127.0.0.1:1234", is_local=True) == "http://127.0.0.1:1234"

    def test_local_http_ipv6_loopback_allowed(self):
        out = validate_endpoint("http://[::1]:8000/v1", is_local=True)
        assert out == "http://[::1]:8000/v1"

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "data:text/plain,hi",
            "javascript:alert(1)",
            "ftp://example.com/x",
            "ws://example.com",
        ],
    )
    def test_bad_scheme_rejected(self, url):
        with pytest.raises(SummaryNetworkError):
            validate_endpoint(url, is_local=False)

    def test_userinfo_rejected(self):
        with pytest.raises(SummaryNetworkError) as e:
            validate_endpoint("https://trusted.example@evil.example/v1", is_local=False)
        assert e.value.category == "invalid_address"

    def test_crlf_rejected(self):
        with pytest.raises(SummaryNetworkError):
            validate_endpoint("https://api.example.com/v1\r\nHost: evil", is_local=False)

    def test_fragment_and_query_rejected(self):
        with pytest.raises(SummaryNetworkError):
            validate_endpoint("https://api.example.com/v1#frag", is_local=False)
        with pytest.raises(SummaryNetworkError):
            validate_endpoint("https://api.example.com/v1?key=secret", is_local=False)

    def test_bad_port_rejected(self):
        with pytest.raises(SummaryNetworkError):
            validate_endpoint("https://api.example.com:99999/v1", is_local=False)

    def test_empty_rejected(self):
        with pytest.raises(SummaryNetworkError):
            validate_endpoint("   ", is_local=False)

    def test_confusable_localhost_subdomain_treated_as_external(self):
        # localhost.evil.example 는 loopback이 아니므로 외부로 판정(거부하지 않고 정규화)
        out = validate_endpoint("https://localhost.evil.example/v1", is_local=False)
        assert out == "https://localhost.evil.example/v1"
        # 로컬 모드로는 loopback이 아니므로 거부
        with pytest.raises(SummaryNetworkError) as e:
            validate_endpoint("http://localhost.evil.example/v1", is_local=True)
        assert e.value.category == "unsafe_address"

    def test_ip_dotted_confusable_external(self):
        # 127.0.0.1.evil.example 는 IP가 아니라 호스트명 → 외부로 판정
        assert validate_endpoint("https://127.0.0.1.evil.example", is_local=False)

    def test_normalization_strips_trailing_slash_and_lowercases_host(self):
        assert validate_endpoint("https://API.Example.com/v1/", is_local=False) == (
            "https://api.example.com/v1"
        )


class TestIpPolicy:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # 클라우드 metadata
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",  # IPv4-mapped 내부
            "::ffff:10.0.0.1",
        ],
    )
    def test_internal_ips_blocked(self, ip):
        assert _ip_is_blocked_for_external(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_ips_allowed(self, ip):
        assert _ip_is_blocked_for_external(ipaddress.ip_address(ip)) is False


class TestAssertHostAllowed:
    def test_external_hostname_resolving_to_internal_blocked(self, monkeypatch):
        import app.services.summary.endpoint as ep

        monkeypatch.setattr(
            ep.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
        )
        with pytest.raises(SummaryNetworkError) as e:
            assert_host_allowed("sneaky.example.com", 443, is_local=False)
        assert e.value.category == "unsafe_address"

    def test_external_hostname_resolving_to_public_ok(self, monkeypatch):
        import app.services.summary.endpoint as ep

        monkeypatch.setattr(
            ep.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        assert_host_allowed("api.example.com", 443, is_local=False)  # 예외 없음

    def test_local_hostname_resolving_to_nonloopback_blocked(self, monkeypatch):
        import app.services.summary.endpoint as ep

        monkeypatch.setattr(
            ep.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("192.168.0.10", 1234))],
        )
        with pytest.raises(SummaryNetworkError) as e:
            assert_host_allowed("printer.local", 1234, is_local=True)
        assert e.value.category == "unsafe_address"


# --- 로컬 HTTP 서버 기반 안전 POST 테스트 (127.0.0.1만) ---


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 테스트 로그 소음 제거
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        path = self.path
        if path == "/ok/chat/completions":
            body = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/redirect/chat/completions":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/other")
            self.end_headers()
        elif path == "/huge/chat/completions":
            body = b"x" * (6 * 1024 * 1024)  # 6MB > 4MB 상한
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass  # 클라이언트가 상한 초과로 조기 종료 — 정상 동작
        elif path == "/notjson/chat/completions":
            body = b"<html>error</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/slow/chat/completions":
            time.sleep(2.0)
            self.send_response(200)
            self.end_headers()
        elif path == "/drip/chat/completions":
            # 전체 데드라인 초과를 유도한다: 응답을 조금씩 흘리며 오래 끈다
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")  # 더 온다고 알리고 채우지 않는다
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, OSError):
                pass
        elif path == "/ctxlen/chat/completions":
            # OpenAI 호환 서버가 컨텍스트 초과를 알리는 유일한 명시적 신호
            body = (
                b'{"error":{"message":"This model\'s maximum context length is 4096 '
                b'tokens","code":"context_length_exceeded"}}'
            )
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/badparam/chat/completions":
            body = b'{"error":{"message":"unknown parameter: foo"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/status"):
            code = int(path.split("/")[2])
            self.send_response(code)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _post(base, path, **kw):
    return post_json(
        f"{base}{path}/chat/completions",
        {"model": "m", "messages": []},
        "secret-key",
        is_local=True,
        timeout=kw.get("timeout", 5.0),
        max_response_bytes=kw.get("max_response_bytes", 4 * 1024 * 1024),
    )


class TestSafePost:
    def test_normal_small_json_ok(self, local_server):
        data = _post(local_server, "/ok")
        assert data["choices"][0]["message"]["content"] == "{}"

    def test_redirect_blocked(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/redirect")
        assert e.value.category == "redirect_blocked"

    def test_oversized_response_rejected(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/huge")
        assert e.value.category == "response_too_large"

    def test_non_json_rejected(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/notjson")
        assert e.value.category == "bad_response"

    def test_context_length_exceeded_is_not_buried_in_bad_response(self, local_server):
        """OpenAI 호환 서버(LM Studio·llama.cpp·vLLM·OpenAI)의 초과 신호는 400 본문뿐이다.

        bad_response로 뭉뚱그리면 executor의 적응 분할이 발동하지 않아 문서 전체가
        즉시 실패하고, 사용자는 실행 가능한 안내 대신 "응답 형식 오류"를 본다.
        """
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/ctxlen")
        assert e.value.category == "context_overflow"
        assert e.value.reason == "http_400_context_length"

    def test_ordinary_bad_request_stays_bad_response(self, local_server):
        """400을 전부 초과로 몰면 파라미터 오류에 분할 재시도를 낭비한다."""
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/badparam")
        assert e.value.category == "bad_response"
        assert e.value.reason == "http_400"

    def test_timeout(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/slow", timeout=0.5)
        assert e.value.category == "timeout"

    def test_slow_drip_hits_total_deadline(self, local_server):
        # 서버가 응답을 조금씩 흘려도 전체 데드라인이 요청을 끝낸다(slow-loris 방어)
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/drip", timeout=0.6)
        assert e.value.category == "timeout"

    @pytest.mark.parametrize(
        "code,category",
        [(401, "auth_failed"), (403, "forbidden"), (404, "model_not_found"),
         (429, "rate_limited"), (500, "server_error")],
    )
    def test_http_status_categories(self, local_server, code, category):
        # /status/<code>/chat/completions
        with pytest.raises(SummaryNetworkError) as e:
            post_json(
                f"{local_server}/status/{code}/chat/completions",
                {"model": "m", "messages": []},
                "secret-key",
                is_local=True,
                timeout=5.0,
                max_response_bytes=4 * 1024 * 1024,
            )
        assert e.value.category == category

    def test_api_key_not_in_error(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/status/401")
        assert "secret-key" not in str(e.value)
        assert "secret-key" not in repr(e.value)
