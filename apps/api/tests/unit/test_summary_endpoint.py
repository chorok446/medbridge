"""요약 endpoint 검증·SSRF 정책·안전 HTTP POST 단위 테스트.

네트워크 관련 테스트는 127.0.0.1 loopback HTTP 서버만 사용한다(외부 접속 없음).
"""

import ipaddress
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.services.summary.cancellation import (
    SummaryCancellationSignal,
    SummaryCancelled,
)
from app.services.summary.endpoint import (
    SummaryNetworkError,
    _ip_is_blocked_for_external,
    assert_host_allowed,
    post_json,
    validate_endpoint,
)

_cancel_body_started = threading.Event()
_cancel_error_body_started = threading.Event()
_cancel_headers_started = threading.Event()
_stream_partial_headers_started = threading.Event()
_stream_blocked_body_started = threading.Event()
_stream_first_line_sent = threading.Event()
_stream_long_tail_sent = threading.Event()


class _BlockingConnectSocket:
    """pending TCP connect를 외부 취소까지 멈추는 결정적 fake socket."""

    def __init__(self, started: threading.Event) -> None:
        self.started = started
        self.released = threading.Event()
        self.aborted = False
        self.closed = False
        self.timeout: object | None = None

    def settimeout(self, value) -> None:
        self.timeout = value

    def bind(self, _source_address) -> None:
        pass

    def connect(self, _socket_address) -> None:
        self.started.set()
        if not self.released.wait(3.0):
            raise TimeoutError("test connect was not released")
        if self.aborted:
            raise ConnectionAbortedError("test socket aborted")

    def shutdown(self, _how) -> None:
        self.aborted = True
        self.released.set()

    def close(self) -> None:
        self.closed = True
        self.released.set()

    def setsockopt(self, *_args) -> None:
        pass


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


class TestPendingConnectionCancellation:
    @pytest.mark.parametrize("scheme", ["http", "https"])
    @pytest.mark.parametrize("abort_kind", ["cancel", "deadline"])
    def test_pending_connect_is_aborted_promptly(
        self, monkeypatch, scheme, abort_kind
    ):
        """HTTP/HTTPS가 self.sock 할당 전에 멈춰도 pending socket을 끊는다."""
        import app.services.summary.endpoint as ep

        connect_started = threading.Event()
        sockets: list[_BlockingConnectSocket] = []

        def create_socket(*_args):
            sock = _BlockingConnectSocket(connect_started)
            sockets.append(sock)
            return sock

        monkeypatch.setattr(
            ep.socket,
            "getaddrinfo",
            lambda *_args, **_kwargs: [
                (
                    ep.socket.AF_INET,
                    ep.socket.SOCK_STREAM,
                    ep.socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443 if scheme == "https" else 80),
                )
            ],
        )
        monkeypatch.setattr(ep.socket, "socket", create_socket)

        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        controller = ep._RequestAbortController(signal)
        connection_class = (
            ep._CancellableHTTPSConnection
            if scheme == "https"
            else ep._CancellableHTTPConnection
        )
        connection = connection_class(
            "api.example.com",
            timeout=10.0,
            abort_controller=controller,
        )
        errors: list[BaseException] = []

        def connect() -> None:
            try:
                connection.connect()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        assert connect_started.wait(1.0), "fake socket did not enter connect"

        started = time.monotonic()
        if abort_kind == "cancel":
            signal.cancel()
        else:
            controller.expire_deadline()
        worker.join(1.5)
        elapsed = time.monotonic() - started

        controller.close()
        assert not worker.is_alive(), "pending connect survived abort callback"
        assert elapsed < 1.0
        assert len(sockets) == 1
        assert sockets[0].aborted is True
        assert len(errors) == 1
        if abort_kind == "cancel":
            assert isinstance(errors[0], SummaryCancelled)
        else:
            assert isinstance(errors[0], SummaryNetworkError)
            assert errors[0].category == "timeout"

    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_created_socket_stays_abortable_until_http_assignment(
        self, monkeypatch, scheme
    ):
        """create_connection 반환값이 self.sock에 할당되기 전의 경합을 막는다."""
        import app.services.summary.endpoint as ep

        connect_started = threading.Event()
        create_returned = threading.Event()
        allow_assignment = threading.Event()
        sockets: list[_BlockingConnectSocket] = []

        def create_socket(*_args):
            sock = _BlockingConnectSocket(connect_started)
            sock.released.set()  # OS connect 자체는 즉시 성공한다.
            sockets.append(sock)
            return sock

        monkeypatch.setattr(
            ep.socket,
            "getaddrinfo",
            lambda *_args, **_kwargs: [
                (
                    ep.socket.AF_INET,
                    ep.socket.SOCK_STREAM,
                    ep.socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443 if scheme == "https" else 80),
                )
            ],
        )
        monkeypatch.setattr(ep.socket, "socket", create_socket)

        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        controller = ep._RequestAbortController(signal)
        connection_class = (
            ep._CancellableHTTPSConnection
            if scheme == "https"
            else ep._CancellableHTTPConnection
        )
        connection = connection_class(
            "api.example.com",
            timeout=10.0,
            abort_controller=controller,
        )
        create_connection = connection._create_connection

        def pause_after_create(*args):
            sock = create_connection(*args)
            create_returned.set()
            assert allow_assignment.wait(2.0)
            return sock

        connection._create_connection = pause_after_create
        errors: list[BaseException] = []

        def connect() -> None:
            try:
                connection.connect()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        assert create_returned.wait(1.0)
        assert connection.sock is None

        signal.cancel()
        assert len(sockets) == 1
        assert sockets[0].aborted is True
        allow_assignment.set()
        worker.join(1.5)

        controller.close()
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)

    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_cancel_after_dns_prevents_socket_creation(self, monkeypatch, scheme):
        """blocking DNS 사이 취소되면 DNS 반환 뒤 connect를 시작하지 않는다."""
        import app.services.summary.endpoint as ep

        dns_started = threading.Event()
        dns_release = threading.Event()
        socket_calls: list[tuple] = []

        def getaddrinfo(*_args, **_kwargs):
            dns_started.set()
            assert dns_release.wait(2.0)
            return [
                (
                    ep.socket.AF_INET,
                    ep.socket.SOCK_STREAM,
                    ep.socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443 if scheme == "https" else 80),
                )
            ]

        def create_socket(*args):
            socket_calls.append(args)
            raise AssertionError("cancelled DNS must not create a socket")

        monkeypatch.setattr(ep.socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(ep.socket, "socket", create_socket)

        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        controller = ep._RequestAbortController(signal)
        connection_class = (
            ep._CancellableHTTPSConnection
            if scheme == "https"
            else ep._CancellableHTTPConnection
        )
        connection = connection_class(
            "api.example.com",
            timeout=10.0,
            abort_controller=controller,
        )
        errors: list[BaseException] = []

        def connect() -> None:
            try:
                connection.connect()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        assert dns_started.wait(1.0)
        signal.cancel()
        dns_release.set()
        worker.join(1.0)

        controller.close()
        assert not worker.is_alive()
        assert socket_calls == []
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)


# --- 로컬 HTTP 서버 기반 안전 POST 테스트 (127.0.0.1만) ---


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 테스트 로그 소음 제거
        pass

    def do_GET(self):  # noqa: N802
        if self.path != "/cancel-metadata":
            self.send_response(404)
            self.end_headers()
            return
        _cancel_headers_started.set()
        try:
            self.wfile.write(b"HTTP/1.1 20")
            self.wfile.flush()
            time.sleep(3.0)
        except (BrokenPipeError, OSError):
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
            self.wfile.flush()
            time.sleep(0.02)
        elif path == "/redirect/chat/completions":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/other")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()
            time.sleep(0.02)
        elif path == "/huge/chat/completions":
            # 실제 6MiB를 한 번의 Windows loopback sendall로 쓰면 송신 완료와
            # 클라이언트 header read가 서로 기다리는 테스트 전용 교착이 생길 수 있다.
            # Content-Length 계약만 크게 선언해 header 단계의 조기 거부를 검증한다.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(6 * 1024 * 1024))
            self.end_headers()
            self.wfile.flush()
            # 선언한 본문을 안 보내는 fixture가 Windows에서 헤더까지
            # RST로 폐기하지 않도록 client의 헤더 파싱 기회를 준다.
            time.sleep(0.02)
        elif path == "/huge-stream/chat/completions":
            # Content-Length를 신뢰할 수 없는 응답도 실제 읽은 byte 상한으로 막는다.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                for _ in range(16):
                    self.wfile.write(b"x" * 256)
                    self.wfile.flush()
                    # Windows loopback에서 송신 직후 handler가 반환되면 아직 읽지 않은
                    # body가 RST로 끝날 수 있다. 클라이언트가 streaming 상한을 먼저
                    # 관측하도록 조각 사이에 짧은 전송 경계를 둔다.
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # 클라이언트가 상한 초과로 조기 종료 — 정상 동작
        elif path == "/notjson/chat/completions":
            body = b"<html>error</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            time.sleep(0.02)
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
        elif path == "/drip400/chat/completions":
            # 400 분류용 오류 본문도 한 raw read씩 읽어 같은 절대 기한을 지켜야 한다.
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, OSError):
                pass
        elif path == "/cancel-body/chat/completions":
            # 헤더 뒤 본문을 오래 멈춰 blocking read1을 만든다. 취소는 전체 timeout을
            # 기다리지 않고 client socket을 shutdown해 이 읽기를 깨워야 한다.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            _cancel_body_started.set()
            time.sleep(3.0)
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, OSError):
                pass
        elif path == "/cancel-body-400/chat/completions":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            _cancel_error_body_started.set()
            time.sleep(3.0)
            try:
                self.wfile.write(b'{"error":{"message":"bad parameter"}}')
            except (BrokenPipeError, OSError):
                pass
        elif path == "/cancel-headers/chat/completions":
            # TCP 연결과 요청 수신은 끝났지만 status/header를 보내지 않는다. custom
            # HTTPConnection callback이 getresponse의 blocking readline을 깨워야 한다.
            _cancel_headers_started.set()
            # 완전한 status line이 아닌 일부를 먼저 보내 BadStatusLine 같은 파서 예외
            # 경로도 취소로 정규화되는지 검증한다.
            self.wfile.write(b"HTTP/1.1 20")
            self.wfile.flush()
            time.sleep(3.0)
            try:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            except (BrokenPipeError, OSError):
                pass
        elif path == "/drip-headers/chat/completions":
            # status line 뒤 끝나지 않는 헤더를 idle timeout보다 빠르게 흘린다. 각 recv가
            # 성공해도 별도 wall-deadline watchdog이 전체 요청 기한에 socket을 끊어야 한다.
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                self.wfile.flush()
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, OSError):
                pass
        elif path == "/stream-partial-headers":
            _stream_partial_headers_started.set()
            try:
                self.wfile.write(b"HTTP/1.1 20")
                self.wfile.flush()
                time.sleep(3.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        elif path == "/stream-blocked-body":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.flush()
            _stream_blocked_body_started.set()
            time.sleep(3.0)
            try:
                self.wfile.write(b'{"status":"late"}\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        elif path == "/stream-first-line":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"status":"first"}\n')
            self.wfile.flush()
            _stream_first_line_sent.set()
            time.sleep(3.0)
            try:
                self.wfile.write(b'{"status":"second"}\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        elif path == "/stream-long-tail":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b"x" * 2048)
            self.wfile.flush()
            _stream_long_tail_sent.set()
            time.sleep(3.0)
            try:
                self.wfile.write(b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
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
            self.wfile.flush()
            time.sleep(0.02)
        elif path == "/badparam/chat/completions":
            body = b'{"error":{"message":"unknown parameter: foo"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            time.sleep(0.02)
        elif path.startswith("/status"):
            code = int(path.split("/")[2])
            self.send_response(code)
            if code == 429:
                self.send_header("Retry-After", "120")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()
            # Windows loopback은 handler가 헤더 송신 즉시 반환하면
            # client가 status를 파싱하기 전에 RST로 관측할 수 있다.
            time.sleep(0.02)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _post(base, path, **kw):
    return post_json(
        f"{base}{path}/chat/completions",
        {"model": "m", "messages": []},
        "secret-key",
        is_local=True,
        timeout=kw.get("timeout", 5.0),
        max_response_bytes=kw.get("max_response_bytes", 4 * 1024 * 1024),
    )


def _stream(base, path, **kwargs):
    from app.services.summary.endpoint import stream_lines

    return stream_lines(
        f"{base}{path}",
        {"stream": True},
        "secret-key",
        is_local=True,
        connect_timeout=kwargs.get("connect_timeout", 2.0),
        idle_timeout=kwargs.get("idle_timeout", 2.0),
        total_deadline=kwargs.get("total_deadline", 5.0),
        max_line_bytes=kwargs.get("max_line_bytes", 64 * 1024),
        max_total_bytes=kwargs.get("max_total_bytes", 1024 * 1024),
        should_cancel=kwargs.get("should_cancel"),
    )


class TestSafeStream:
    def test_already_cancelled_does_not_open_network(self, monkeypatch):
        """generator 시작 시 이미 취소됐으면 opener를 생성·실행하지 않는다."""
        import app.services.summary.endpoint as ep

        build_calls = []
        policy_calls = []

        def build_opener(*_args, **_kwargs):
            build_calls.append(True)
            raise AssertionError("cancelled stream must not build or open a transport")

        def assert_url_allowed(*_args, **_kwargs):
            policy_calls.append(True)
            raise AssertionError("cancelled stream must not start DNS policy lookup")

        monkeypatch.setattr(ep, "_build_opener", build_opener)
        monkeypatch.setattr(ep, "_assert_url_allowed", assert_url_allowed)

        assert (
            list(
                _stream(
                    "http://127.0.0.1:1",
                    "/stream",
                    should_cancel=lambda: True,
                )
            )
            == []
        )
        assert build_calls == []
        assert policy_calls == []

    @pytest.mark.parametrize(
        ("path", "started_event"),
        [
            ("/stream-partial-headers", _stream_partial_headers_started),
            ("/stream-blocked-body", _stream_blocked_body_started),
        ],
    )
    def test_cancel_interrupts_blocked_header_and_body_silently(
        self, local_server, path, started_event
    ):
        """pull/Q&A 취소는 100ms watcher로 socket을 깨우고 정상 종료한다."""

        started_event.clear()
        cancelled = threading.Event()
        lines: list[str] = []
        errors: list[BaseException] = []

        def consume() -> None:
            try:
                lines.extend(
                    _stream(
                        local_server,
                        path,
                        connect_timeout=10.0,
                        idle_timeout=10.0,
                        total_deadline=10.0,
                        should_cancel=cancelled.is_set,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        assert started_event.wait(1.5), "fixture did not enter blocked stream I/O"

        started = time.monotonic()
        cancelled.set()
        worker.join(1.5)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "stream cancel waited for the socket timeout"
        assert elapsed < 1.0
        assert lines == []
        assert errors == []

    @pytest.mark.parametrize(
        ("connect_timeout", "total_deadline"),
        [(0.4, 2.0), (2.0, 0.4)],
    )
    def test_slow_drip_headers_hit_connect_or_total_wall_deadline(
        self, local_server, connect_timeout, total_deadline
    ):
        """idle 내부로 header를 흘려도 connect/total wall 상한을 넘지 못한다."""

        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as error:
            list(
                _stream(
                    local_server,
                    "/drip-headers/chat/completions",
                    connect_timeout=connect_timeout,
                    idle_timeout=1.0,
                    total_deadline=total_deadline,
                )
            )
        elapsed = time.monotonic() - started

        assert error.value.category == "timeout"
        assert elapsed < 1.5

    def test_slow_drip_body_hits_total_wall_deadline(self, local_server):
        """read1이 작은 body를 계속 받아도 전체 기한이 socket을 끊는다."""

        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as error:
            list(
                _stream(
                    local_server,
                    "/drip/chat/completions",
                    connect_timeout=2.0,
                    idle_timeout=0.5,
                    total_deadline=0.6,
                )
            )
        elapsed = time.monotonic() - started

        assert error.value.category == "timeout"
        assert elapsed < 1.5

    def test_first_complete_line_yields_before_8192_bytes_or_eof(self, local_server):
        """NDJSON/SSE의 첫 완결 줄을 다음 8192 bytes나 EOF까지 보류하지 않는다."""

        _stream_first_line_sent.clear()
        iterator = _stream(
            local_server,
            "/stream-first-line",
            connect_timeout=2.0,
            idle_timeout=5.0,
            total_deadline=5.0,
        )
        lines: list[str] = []
        errors: list[BaseException] = []

        def read_first() -> None:
            try:
                lines.append(next(iterator))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=read_first, daemon=True)
        started = time.monotonic()
        worker.start()
        assert _stream_first_line_sent.wait(1.5)
        worker.join(1.0)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "first line waited for stream EOF"
        assert errors == []
        assert lines == ['{"status":"first"}']
        assert elapsed < 1.0
        iterator.close()

    def test_unterminated_line_hits_line_cap_before_total_cap_or_eof(self, local_server):
        """newline 없는 현재 줄은 max_total이 아닌 max_line 즉시 거부된다."""

        _stream_long_tail_sent.clear()
        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as error:
            list(
                _stream(
                    local_server,
                    "/stream-long-tail",
                    idle_timeout=5.0,
                    total_deadline=5.0,
                    max_line_bytes=1024,
                    max_total_bytes=1024 * 1024,
                )
            )
        elapsed = time.monotonic() - started

        assert _stream_long_tail_sent.is_set()
        assert error.value.category == "response_too_large"
        assert elapsed < 1.0

    def test_http_error_is_classified_and_closed(self, monkeypatch):
        """header 단계 HTTPError는 기존 status 분류를 유지하고 항상 닫힌다."""
        import urllib.error

        import app.services.summary.endpoint as ep

        closed = threading.Event()

        class TrackingHTTPError(urllib.error.HTTPError):
            def close(self):
                closed.set()

        error = TrackingHTTPError(
            "http://127.0.0.1:1/stream",
            429,
            "rate limited",
            {"Retry-After": "3"},
            None,
        )
        controller = ep._RequestAbortController(None)

        class ErrorOpener:
            _summary_abort_controller = controller

            def open(self, _req, *, timeout):
                assert timeout > 0
                raise error

        monkeypatch.setattr(ep, "_build_opener", lambda *_args, **_kwargs: ErrorOpener())

        with pytest.raises(SummaryNetworkError) as raised:
            list(_stream("http://127.0.0.1:1", "/stream"))

        assert raised.value.category == "rate_limited"
        assert raised.value.retry_after_seconds == 3.0
        assert closed.is_set()

    def test_cancel_wins_over_http_error_and_still_closes(self, monkeypatch):
        """HTTPError와 취소가 경합하면 pull/Q&A iterator는 예외 없이 닫힌다."""
        import urllib.error

        import app.services.summary.endpoint as ep

        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        closed = threading.Event()

        class TrackingHTTPError(urllib.error.HTTPError):
            def close(self):
                closed.set()

        error = TrackingHTTPError("http://127.0.0.1:1/stream", 500, "server error", {}, None)
        controller = ep._RequestAbortController(None)

        class ErrorAfterCancelOpener:
            _summary_abort_controller = controller

            def open(self, _req, *, timeout):
                assert timeout > 0
                unregister = controller.register_abort(release.set)
                try:
                    entered.set()
                    assert release.wait(2.0)
                    raise error
                finally:
                    unregister()

        monkeypatch.setattr(ep, "_build_opener", lambda *_args, **_kwargs: ErrorAfterCancelOpener())
        lines: list[str] = []
        errors: list[BaseException] = []

        def consume() -> None:
            try:
                lines.extend(
                    _stream(
                        "http://127.0.0.1:1",
                        "/stream",
                        should_cancel=cancelled.is_set,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        assert entered.wait(1.0)
        cancelled.set()
        worker.join(1.5)

        assert not worker.is_alive()
        assert lines == []
        assert errors == []
        assert closed.is_set()

    def test_normal_completion_cleans_response_watcher_timers_and_controller(self, monkeypatch):
        """정상 EOF도 watcher/timer/callback/socket을 남기지 않는다."""
        import app.services.summary.endpoint as ep

        timers = []
        response_closed = threading.Event()

        class TrackingTimer:
            def __init__(self, interval, function):
                self.interval = interval
                self.function = function
                self.daemon = False
                self.started = False
                self.cancelled = False
                self.joined = False
                timers.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

            def is_alive(self):
                return self.started and not self.joined

            def join(self, timeout=None):
                assert timeout == 0.2
                self.joined = True

        class OneLineResponse:
            headers = {}

            def __init__(self):
                self.chunks = iter([b'{"status":"ok"}\n', b""])

            def read1(self, _size):
                return next(self.chunks)

            def close(self):
                response_closed.set()

        controller = ep._RequestAbortController(None)

        class OneLineOpener:
            _summary_abort_controller = controller

            def open(self, _req, *, timeout):
                assert timeout > 0
                return OneLineResponse()

        monkeypatch.setattr(ep.threading, "Timer", TrackingTimer)
        monkeypatch.setattr(ep, "_build_opener", lambda *_args, **_kwargs: OneLineOpener())

        assert list(
            _stream(
                "http://127.0.0.1:1",
                "/stream",
                should_cancel=lambda: False,
            )
        ) == ['{"status":"ok"}']

        assert len(timers) == 2
        assert all(timer.started and timer.cancelled and timer.joined for timer in timers)
        assert response_closed.is_set()
        assert controller._abort_callbacks == {}
        assert not any(
            thread.name == "summary-stream-cancel" and thread.is_alive()
            for thread in threading.enumerate()
        )


class TestSafePost:
    def test_normal_small_json_ok(self, local_server):
        data = _post(local_server, "/ok")
        assert data["choices"][0]["message"]["content"] == "{}"

    def test_redirect_blocked(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/redirect")
        assert e.value.category == "redirect_blocked", repr(e.value.__cause__)

    def test_oversized_response_rejected(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/huge")
        assert e.value.category == "response_too_large"

    def test_streamed_oversized_response_rejected(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/huge-stream", max_response_bytes=1024)
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
        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/drip", timeout=0.6)
        elapsed = time.monotonic() - started
        assert e.value.category == "timeout"
        # Windows CI 스케줄링 여유를 포함해도, 4초짜리 drip 전체를 기다리면 안 된다.
        assert elapsed < 2.0

    def test_slow_drip_http_error_body_hits_total_deadline(self, local_server):
        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/drip400", timeout=0.6)
        elapsed = time.monotonic() - started
        assert e.value.category == "timeout"
        assert elapsed < 2.0

    @pytest.mark.parametrize(
        ("path", "body_started"),
        [
            ("/cancel-body", _cancel_body_started),
            ("/cancel-body-400", _cancel_error_body_started),
        ],
    )
    def test_cancellation_interrupts_blocking_response_body_promptly(
        self, local_server, path, body_started
    ):
        """정상/HTTPError 본문 모두 전체 timeout 전에 socket shutdown으로 멈춘다."""

        body_started.clear()
        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        errors: list[BaseException] = []

        def request() -> None:
            try:
                post_json(
                    f"{local_server}{path}/chat/completions",
                    {"model": "m", "messages": []},
                    "secret-key",
                    is_local=True,
                    timeout=10.0,
                    max_response_bytes=4 * 1024 * 1024,
                    cancellation_signal=signal,
                )
            except BaseException as exc:  # 테스트 thread의 결과를 본 thread에서 단언
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        assert body_started.wait(2.0), "server did not enter the blocked response body"

        started = time.monotonic()
        signal.cancel()
        worker.join(1.5)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "cancel left urllib blocked until its full timeout"
        assert elapsed < 1.0
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)

    def test_cancellation_interrupts_response_header_wait_promptly(self, local_server):
        """status/header를 기다리는 opener.open도 전체 cold-load timeout을 기다리지 않는다."""

        _cancel_headers_started.clear()
        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        errors: list[BaseException] = []

        def request() -> None:
            try:
                post_json(
                    f"{local_server}/cancel-headers/chat/completions",
                    {"model": "m", "messages": []},
                    "secret-key",
                    is_local=True,
                    timeout=10.0,
                    max_response_bytes=4 * 1024 * 1024,
                    cancellation_signal=signal,
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        assert _cancel_headers_started.wait(2.0), "server did not enter the header wait"

        started = time.monotonic()
        signal.cancel()
        worker.join(1.5)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "cancel left opener.open blocked until its full timeout"
        assert elapsed < 1.0
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)

    def test_slow_drip_headers_hit_total_deadline(self, local_server):
        """opener.open 내부의 헤더 slow-drip도 전체 wall deadline을 우회하지 못한다."""

        started = time.monotonic()
        with pytest.raises(SummaryNetworkError) as error:
            _post(local_server, "/drip-headers", timeout=0.6)
        elapsed = time.monotonic() - started

        assert error.value.category == "timeout"
        assert elapsed < 2.0

    def test_cancel_interrupts_blocked_get_metadata_probe(self, local_server):
        """Ollama /api/tags 같은 GET identity probe도 worker pool을 timeout까지 점유하지 않는다."""

        from app.services.summary.endpoint import get_json

        _cancel_headers_started.clear()
        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        errors: list[BaseException] = []

        def request() -> None:
            try:
                get_json(
                    f"{local_server}/cancel-metadata",
                    is_local=True,
                    timeout=10.0,
                    max_response_bytes=1024,
                    cancellation_signal=signal,
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        assert _cancel_headers_started.wait(2.0)
        signal.cancel()
        worker.join(1.5)

        assert not worker.is_alive(), "cancel left metadata GET worker blocked"
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)

    def test_urlerror_timeout_reason_is_classified_as_timeout(self):
        """urllib가 socket timeout을 URLError.reason으로 감싸도 timeout을 유지한다."""
        import urllib.error

        import app.services.summary.endpoint as ep

        class TimeoutOpener:
            def open(self, _req, *, timeout):
                assert timeout > 0
                # Python 3.10+에서 socket.timeout은 TimeoutError의 alias다.
                raise urllib.error.URLError(TimeoutError("socket timed out"))

        with pytest.raises(SummaryNetworkError) as error:
            ep._read_capped(
                TimeoutOpener(),
                object(),
                timeout=1.0,
                max_response_bytes=1024,
            )

        assert error.value.category == "timeout"

    def test_urlerror_connection_reset_stays_connect_failed(self):
        """서버 종료 RST를 timeout으로 숨기지 않아 실제 원인을 보존한다."""
        import urllib.error

        import app.services.summary.endpoint as ep

        class ResetOpener:
            def open(self, _req, *, timeout):
                assert timeout > 0
                raise urllib.error.URLError(ConnectionResetError("server reset"))

        with pytest.raises(SummaryNetworkError) as error:
            ep._read_capped(
                ResetOpener(),
                object(),
                timeout=1.0,
                max_response_bytes=1024,
            )

        assert error.value.category == "connect_failed"

    def test_delayed_headers_and_body_share_one_absolute_deadline(self, monkeypatch):
        """헤더 시간 뒤 본문 기한을 다시 시작하지 않고 매 read를 남은 시간으로 줄인다."""
        import app.services.summary.endpoint as ep

        now = [100.0]
        read_calls = [0]
        socket_timeouts: list[float] = []
        open_timeouts: list[float] = []

        class FakeSocket:
            def settimeout(self, value):
                socket_timeouts.append(value)

        class FakeRaw:
            _sock = FakeSocket()

        class FakeFp:
            raw = FakeRaw()

        class DelayedResponse:
            headers: dict[str, str] = {}
            fp = FakeFp()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read1(self, _size):
                read_calls[0] += 1
                if read_calls[0] == 1:
                    now[0] += 0.2
                    return b"{"
                if read_calls[0] == 2:
                    now[0] += 0.45
                    return b"}"
                return b""

            def read(self, _size):
                raise AssertionError("production urllib path must prefer bounded read1")

        class DelayedHeaderOpener:
            def open(self, _req, *, timeout):
                open_timeouts.append(timeout)
                now[0] += 0.4  # 연결 + 응답 헤더 수신
                return DelayedResponse()

        class FakeTime:
            @staticmethod
            def monotonic():
                return now[0]

        monkeypatch.setattr(ep, "time", FakeTime)

        with pytest.raises(SummaryNetworkError) as error:
            ep._read_capped(
                DelayedHeaderOpener(),
                object(),
                timeout=1.0,
                max_response_bytes=1024,
            )

        assert error.value.category == "timeout"
        assert open_timeouts == pytest.approx([1.0])
        assert socket_timeouts == pytest.approx([0.6, 0.4])

    def test_http_error_body_read_uses_remaining_deadline(self, monkeypatch):
        """400 분류용 본문 읽기도 성공 응답과 같은 절대 기한을 넘길 수 없다."""
        import app.services.summary.endpoint as ep

        now = [200.0]
        socket_timeouts: list[float] = []

        def read_error_body(_size):
            now[0] += 0.6
            return b'{"error":{"code":"context_length_exceeded"}}'

        fake_error = SimpleNamespace(
            code=400,
            headers={},
            fp=SimpleNamespace(
                raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=socket_timeouts.append))
            ),
            read1=read_error_body,
            read=lambda _size: pytest.fail("HTTPError body must prefer bounded read1"),
        )

        class FakeTime:
            @staticmethod
            def monotonic():
                return now[0]

        monkeypatch.setattr(ep, "time", FakeTime)

        with pytest.raises(SummaryNetworkError) as error:
            ep._classify_http_error(fake_error, deadline=200.5)

        assert error.value.category == "timeout"
        assert socket_timeouts == pytest.approx([0.5])

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
        assert e.value.category == category, repr(e.value.__cause__)

    def test_rate_limit_preserves_retry_after_without_response_body(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/status/429")

        assert e.value.category == "rate_limited", repr(e.value.__cause__)
        assert e.value.retry_after_seconds == 120.0

    def test_api_key_not_in_error(self, local_server):
        with pytest.raises(SummaryNetworkError) as e:
            _post(local_server, "/status/401")
        assert "secret-key" not in str(e.value)
        assert "secret-key" not in repr(e.value)
