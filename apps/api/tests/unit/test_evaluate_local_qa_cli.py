"""evaluate_local_qa CLI의 출시 모드 격리·fail-closed 계약."""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_local_qa.py"
_SPEC = importlib.util.spec_from_file_location("evaluate_local_qa_cli", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)


@pytest.mark.parametrize(
    "argv",
    [
        ["--fail-on-gate", "--repeat", "2"],
        ["--fail-on-gate", "--repeat", "3", "--allow-skip"],
        ["--fail-on-gate", "--repeat", "3", "--provider", "deterministic"],
        ["--fail-on-gate", "--repeat", "3", "--model", "qwen3:4b"],
        ["--fail-on-gate", "--repeat", "3", "--category", "grounded_basic"],
    ],
)
def test_release_cli_rejects_incomplete_or_incompatible_options_before_run(monkeypatch, argv):
    called = False

    async def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2
    assert called is False


def test_only_exact_fail_on_gate_shape_is_release_mode():
    release = cli._parse_args(["--fail-on-gate", "--repeat", "3"])
    exploratory = cli._parse_args(["--provider", "local", "--model", "qwen3:8b"])

    assert cli._is_release_evaluation(release) is True
    assert cli._is_release_evaluation(exploratory) is False


def test_main_overrides_user_database_and_provider_before_run(monkeypatch):
    captured = {}
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///C:/Users/example/user.db")
    monkeypatch.setenv("QA_PROVIDER", "deterministic")

    async def fake_run(_args, **kwargs):
        captured["database"] = os.environ["DATABASE_URL"]
        captured["provider"] = os.environ["QA_PROVIDER"]
        captured["app_data"] = os.environ["MEDBRIDGE_APP_DATA_DIR"]
        captured["expected"] = kwargs["expected_database_url"]
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    assert cli.main(["--provider", "deterministic"]) == 0
    assert captured["database"] == captured["expected"]
    assert captured["database"] != "sqlite+aiosqlite:///C:/Users/example/user.db"
    assert captured["database"].startswith("sqlite+aiosqlite:///")
    assert captured["provider"] == "auto"
    assert "medbridge-qaeval-" in captured["app_data"]


def test_loaded_settings_must_match_forced_environment():
    settings = SimpleNamespace(
        database_url="sqlite+aiosqlite:///C:/temp/eval.db",
        qa_provider="auto",
    )
    cli._verify_loaded_settings(settings, "sqlite+aiosqlite:///C:/temp/eval.db")

    settings.qa_provider = "deterministic"
    with pytest.raises(cli.ReleaseEvaluationError, match="QA_PROVIDER"):
        cli._verify_loaded_settings(settings, "sqlite+aiosqlite:///C:/temp/eval.db")

    settings.qa_provider = "auto"
    settings.database_url = "sqlite+aiosqlite:///C:/Users/example/user.db"
    with pytest.raises(cli.ReleaseEvaluationError, match="DATABASE_URL"):
        cli._verify_loaded_settings(settings, "sqlite+aiosqlite:///C:/temp/eval.db")


async def test_stable_tag_but_different_loaded_digest_never_writes_report(monkeypatch):
    from alembic import command
    from app.core import config
    from app.db import session
    from app.qa_eval import manifest, ollama, provenance, report, run_case, runner
    from app.services.summary import secrets

    expected_database = "sqlite+aiosqlite:///C:/temp/eval.db"
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url=expected_database, qa_provider="auto"),
    )
    monkeypatch.setattr(session, "get_session_factory", lambda: object())
    monkeypatch.setattr(command, "upgrade", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(secrets, "use_in_memory_backend", lambda: None)

    async def ready(_model):
        return True, ""

    async def materialize(_factory, *, model):
        assert model == "qwen3:8b"

    monkeypatch.setattr(ollama, "preflight", ready)
    monkeypatch.setattr(run_case, "materialize_and_verify_local_runtime", materialize)
    monkeypatch.setattr(manifest, "load_dataset", lambda _path: object())

    baseline = "sha256:" + "b" * 64

    async def read_tag(_model):
        return baseline

    async def read_loaded(_model):
        return "sha256:" + "c" * 64

    guard = provenance.ReleaseGuard(
        Path("C:/temp/repo"),
        provenance.GitSnapshot("a" * 40),
        baseline,
        read_tag,
        read_loaded,
    )
    monkeypatch.setattr(
        provenance,
        "capture_git_snapshot",
        lambda _root: provenance.GitSnapshot("a" * 40),
    )

    async def create_guard(*_args, **_kwargs):
        return guard

    async def fail_at_boundary(*_args, run_boundary, **_kwargs):
        await run_boundary("before", "case", 0)
        await run_boundary("after", "case", 0)
        raise AssertionError("unreachable")

    wrote = False

    def write_reports(*_args, **_kwargs):
        nonlocal wrote
        wrote = True

    monkeypatch.setattr(provenance.ReleaseGuard, "create", create_guard)
    monkeypatch.setattr(runner, "run_evaluation", fail_at_boundary)
    monkeypatch.setattr(report, "write_reports", write_reports)

    args = cli._parse_args(["--fail-on-gate", "--repeat", "3"])
    with pytest.raises(provenance.ReleaseProvenanceError, match="loaded.*digest"):
        await cli._run(
            args,
            expected_database_url=expected_database,
            initial_git_snapshot=provenance.GitSnapshot("a" * 40),
        )
    assert wrote is False
