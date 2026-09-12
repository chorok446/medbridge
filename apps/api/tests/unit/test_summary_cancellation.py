"""요약 작업별 협력 취소 신호의 격리·retry 중단·수명주기 테스트."""

import threading
import uuid

import pytest

from app.services.summary.cancellation import (
    SummaryCancellationSignal,
    SummaryCancelled,
    active_summary_cancellation_count,
    discard_summary_cancellation,
    register_summary_cancellation,
    request_summary_cancellation,
    summary_cancellation_scope,
)
from app.services.summary.endpoint import SummaryNetworkError
from app.services.summary.provider import (
    OpenAICompatibleSummaryProvider,
    new_provider_request_budget,
)
from app.services.tasks.runner import LocalTaskRunner


class TestCancellationRegistry:
    def test_exact_job_and_run_are_isolated(self):
        baseline = active_summary_cancellation_count()
        job_a, job_b = uuid.uuid4(), uuid.uuid4()
        run_a1, run_a2, run_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        signal_a1 = register_summary_cancellation(job_a, run_a1)
        signal_a2 = register_summary_cancellation(job_a, run_a2)
        signal_b = register_summary_cancellation(job_b, run_b)
        try:
            assert request_summary_cancellation(job_a, run_a1) == 1
            assert signal_a1.is_cancelled()
            assert not signal_a2.is_cancelled()
            assert not signal_b.is_cancelled()

            assert request_summary_cancellation(job_b, run_b) == 1
            assert signal_b.is_cancelled()
            assert not signal_a2.is_cancelled()
        finally:
            for signal in (signal_a1, signal_a2, signal_b):
                discard_summary_cancellation(signal)
        assert active_summary_cancellation_count() == baseline

    async def test_runner_completion_discards_registered_signal(self, monkeypatch):
        baseline = active_summary_cancellation_count()
        runner = LocalTaskRunner(max_concurrent=1)
        finished = []
        captured_signal: list[SummaryCancellationSignal] = []

        async def finish_immediately(*_args):
            finished.append(True)

        original_register = register_summary_cancellation

        def capture_register(job_id, run_id):
            signal = original_register(job_id, run_id)
            captured_signal.append(signal)
            return signal

        from app.services.summary import cancellation as cancellation_mod

        monkeypatch.setattr(runner, "_run_summary", finish_immediately)
        monkeypatch.setattr(cancellation_mod, "register_summary_cancellation", capture_register)
        runner.enqueue_summary(
            uuid.uuid4(),
            "cancel-cleanup",
            run_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
        )
        assert active_summary_cancellation_count() == baseline + 1

        await runner.drain()

        assert finished == [True]
        assert captured_signal[0].is_cancelled(), "finished task left a worker cancellation open"
        assert active_summary_cancellation_count() == baseline


class TestProviderCancellation:
    @staticmethod
    def _provider(http_client) -> OpenAICompatibleSummaryProvider:
        return OpenAICompatibleSummaryProvider(
            endpoint="http://127.0.0.1:11434/v1",
            model_name="qwen",
            api_key="",
            is_local=True,
            http_client=http_client,
        )

    def test_cancelled_before_request_never_calls_transport(self):
        calls = []
        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        signal.cancel()
        provider = self._provider(lambda *_args: calls.append(True))

        with summary_cancellation_scope(signal), pytest.raises(SummaryCancelled):
            provider._request_json(  # noqa: SLF001 — actual retry boundary contract
                "http://127.0.0.1:11434/api/chat",
                {},
                is_local=True,
                budget=new_provider_request_budget(total_deadline_seconds=60.0),
            )

        assert calls == []

    def test_cancel_during_retry_backoff_prevents_another_provider_call(
        self, monkeypatch
    ):
        from app.services.summary import provider as provider_mod

        first_call = threading.Event()
        calls = []
        signal = SummaryCancellationSignal(job_id=uuid.uuid4(), run_id=uuid.uuid4())
        errors: list[BaseException] = []

        def transient_failure(*_args):
            calls.append(True)
            first_call.set()
            raise SummaryNetworkError("connect_failed")

        provider = self._provider(transient_failure)
        monkeypatch.setattr(provider_mod, "_retry_delay", lambda *_args: 30.0)

        def invoke() -> None:
            try:
                with summary_cancellation_scope(signal):
                    provider._request_json(  # noqa: SLF001 — actual retry boundary contract
                        "http://127.0.0.1:11434/api/chat",
                        {},
                        is_local=True,
                        budget=new_provider_request_budget(total_deadline_seconds=60.0),
                    )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        assert first_call.wait(1.0)
        signal.cancel()
        worker.join(1.0)

        assert not worker.is_alive(), "cancel did not interrupt provider retry backoff"
        assert calls == [True]
        assert len(errors) == 1
        assert isinstance(errors[0], SummaryCancelled)
