"""로그 잡음 필터 — 오류 보고서의 로그 꼬리가 폴링에 밀려나지 않게 한다.

실기기 근거: 오류 보고서 logTail 200줄 중 163줄이 `/summaries/status` 폴링이었고,
진단에 쓸 수 있는 줄은 1줄뿐이었다. 화면은 진행 중인 작업을 1~1.5초마다 폴링하므로
작업이 몇 분만 돌아도 200줄 창이 폴링으로 가득 찬다.
"""

import logging

from app.core.logging import install_log_noise_filters


def _access_record(message: str) -> logging.LogRecord:
    """uvicorn.access가 만드는 것과 같은 형태의 레코드."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", *message.split("|")),
        exc_info=None,
    )


def _passes(logger_name: str, record: logging.LogRecord) -> bool:
    return all(f.filter(record) for f in logging.getLogger(logger_name).filters)


class TestAccessLogNoise:
    def setup_method(self):
        install_log_noise_filters()

    def test_drops_successful_status_polling(self):
        doc = "/api/documents/5fb5885d-e7b1-459d-b402-a36b5bc321ab"
        for path in (
            f"{doc}/summaries/status",
            f"{doc}/chunks/status",
            # 실제 라우트는 하이픈이다. 슬래시로 적으면 존재하지 않는 경로를 검사해
            # 필터가 망가져도 테스트가 통과한다(이 파일이 실제로 그랬다).
            f"{doc}/ocr-status",
            f"{doc}/extraction-status",
            f"{doc}/jobs",
        ):
            record = _access_record(f"GET|{path}|1.1|200")
            assert not _passes("uvicorn.access", record), f"폴링이 남았다: {path}"

    def test_every_status_route_in_the_app_is_covered(self):
        """경로를 손으로 적으면 라우트가 바뀔 때 조용히 어긋난다.

        실제로 어긋났다 — 필터는 `/status`만 보는데 라우트는 `-status`였고, 테스트는
        존재하지 않는 `ocr/status`를 검사해 통과했다. 오류 보고서의 logTail은 여전히
        폴링으로 가득 찼는데 CI는 초록이었다. 라우트 표에서 직접 뽑아 그 구멍을 막는다.
        """
        from app.api.routes import extraction, ocr, search, summary

        paths = [
            route.path
            for module in (ocr, extraction, search, summary)
            for route in module.router.routes
            if getattr(route, "path", "").endswith("status")
        ]
        assert paths, "status 라우트를 하나도 찾지 못했다 — 이 단언이 공허하다"

        for path in paths:
            concrete = path.replace("{document_id}", "5fb5885d-e7b1-459d-b402-a36b5bc321ab")
            record = _access_record(f"GET|/api/documents{concrete}|1.1|200")
            assert not _passes("uvicorn.access", record), f"필터가 놓친 폴링 경로: {path}"

    def test_drops_successful_preflight(self):
        record = _access_record("OPTIONS|/api/profile|1.1|200")
        assert not _passes("uvicorn.access", record)

    def test_keeps_failed_status_polling(self):
        """실패한 폴링은 남긴다 — 이건 진단에 필요한 신호다."""
        doc = "/api/documents/5fb5885d-e7b1-459d-b402-a36b5bc321ab"
        for status in (409, 500):
            record = _access_record(f"GET|{doc}/summaries/status|1.1|{status}")
            assert _passes("uvicorn.access", record), f"{status}가 사라졌다"

    def test_keeps_non_polling_requests(self):
        """폴링이 아닌 요청은 성공이라도 남긴다 — 사용자 행동의 흔적이다."""
        doc = "/api/documents/5fb5885d-e7b1-459d-b402-a36b5bc321ab"
        for method, path in (
            ("POST", f"{doc}/summaries"),
            ("POST", "/api/documents"),
            ("GET", f"{doc}/summaries"),
            ("DELETE", doc),
        ):
            record = _access_record(f"{method}|{path}|1.1|200")
            assert _passes("uvicorn.access", record), f"사라지면 안 된다: {method} {path}"

    def test_keeps_application_logs(self):
        """구조화 로그(app 로거)는 필터 대상이 아니다."""
        record = logging.LogRecord(
            name="app.services.tasks.summary_job",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='{"event": "summary_job_done"}',
            args=(),
            exc_info=None,
        )
        assert _passes("app.services.tasks.summary_job", record)


class TestClientDisconnectNoise:
    def setup_method(self):
        install_log_noise_filters()

    def _disconnect_record(self, winerror: int) -> logging.LogRecord:
        exc = ConnectionResetError(winerror, "현재 연결은 원격 호스트에 의해 강제로 끊겼습니다")
        exc.winerror = winerror
        try:
            raise exc
        except ConnectionResetError:
            import sys

            exc_info = sys.exc_info()
        return logging.LogRecord(
            name="asyncio",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            args=(),
            exc_info=exc_info,
        )

    def test_drops_client_disconnect(self):
        """뷰어가 PDF 요청을 끊으면 나는 정상적인 disconnect다.

        5줄짜리 트레이스백으로 남으면 로그 꼬리를 잡아먹는다.
        """
        assert not _passes("asyncio", self._disconnect_record(10054))

    def test_keeps_other_asyncio_errors(self):
        record = logging.LogRecord(
            name="asyncio",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Task exception was never retrieved",
            args=(),
            exc_info=None,
        )
        assert _passes("asyncio", record)


def test_install_is_idempotent():
    """기동 경로가 두 번 불러도 필터가 쌓이지 않는다."""
    install_log_noise_filters()
    first = len(logging.getLogger("uvicorn.access").filters)
    install_log_noise_filters()
    install_log_noise_filters()
    assert len(logging.getLogger("uvicorn.access").filters) == first
