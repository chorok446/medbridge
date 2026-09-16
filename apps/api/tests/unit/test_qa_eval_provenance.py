"""출시 평가의 Git·모델 provenance가 fail-closed인지 검증한다."""

import subprocess

import pytest

from app.qa_eval import provenance

HEAD = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_git_snapshot_reads_head_status_head_and_allows_only_codex_hooks(monkeypatch, tmp_path):
    responses = iter(
        [
            _completed(HEAD + "\n"),
            _completed("?? .codex/hooks.json\0"),
            _completed(HEAD + "\n"),
        ]
    )
    calls = []

    def fake_git(repo_root, *args):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(provenance, "_git", fake_git)

    snapshot = provenance.capture_git_snapshot(tmp_path)

    assert snapshot.tested_commit == HEAD
    assert calls == [
        ("rev-parse", "--verify", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("rev-parse", "--verify", "HEAD"),
    ]


@pytest.mark.parametrize(
    "status",
    [
        " M apps/api/app/main.py\0",
        "?? apps/api/app/injected.py\0",
        "?? .env\0",
        "?? .codex/other.json\0",
        "?? .codex/hooks.json\0?? tmp/injected.py\0",
    ],
)
def test_git_snapshot_rejects_every_other_worktree_change(monkeypatch, tmp_path, status):
    responses = iter([_completed(HEAD + "\n"), _completed(status), _completed(HEAD + "\n")])
    monkeypatch.setattr(provenance, "_git", lambda *_args: next(responses))

    with pytest.raises(provenance.ReleaseProvenanceError, match="worktree"):
        provenance.capture_git_snapshot(tmp_path)


def test_git_snapshot_rejects_command_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda *_args: _completed(returncode=128, stderr="not a repository"),
    )

    with pytest.raises(provenance.ReleaseProvenanceError, match="Git"):
        provenance.capture_git_snapshot(tmp_path)


def test_git_snapshot_rejects_head_change_during_capture(monkeypatch, tmp_path):
    responses = iter(
        [_completed(HEAD + "\n"), _completed(""), _completed("c" * 40 + "\n")]
    )
    monkeypatch.setattr(provenance, "_git", lambda *_args: next(responses))

    with pytest.raises(provenance.ReleaseProvenanceError, match="HEAD"):
        provenance.capture_git_snapshot(tmp_path)


async def test_release_guard_detects_digest_change_and_repository_change(monkeypatch, tmp_path):
    snapshots = iter(
        [
            provenance.GitSnapshot(HEAD),
            provenance.GitSnapshot("c" * 40),
        ]
    )
    digests = iter([DIGEST, DIGEST, "sha256:" + "d" * 64])
    monkeypatch.setattr(provenance, "capture_git_snapshot", lambda _root: next(snapshots))

    async def read_digest(_model):
        return next(digests)

    guard = await provenance.ReleaseGuard.create(tmp_path, read_digest=read_digest)
    await guard.verify_digest("before-run")
    with pytest.raises(provenance.ReleaseProvenanceError, match="digest"):
        await guard.verify_digest("after-run")
    with pytest.raises(provenance.ReleaseProvenanceError, match="HEAD"):
        guard.verify_repository()


async def test_release_guard_rejects_invalid_initial_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(
        provenance, "capture_git_snapshot", lambda _root: provenance.GitSnapshot(HEAD)
    )

    async def read_digest(_model):
        return "not-a-digest"

    with pytest.raises(provenance.ReleaseProvenanceError, match="digest"):
        await provenance.ReleaseGuard.create(tmp_path, read_digest=read_digest)


async def test_run_after_rejects_loaded_digest_even_when_tag_returns_to_baseline(tmp_path):
    async def read_tag(_model):
        return DIGEST

    async def read_loaded(_model):
        return "sha256:" + "c" * 64

    guard = provenance.ReleaseGuard(
        tmp_path,
        provenance.GitSnapshot(HEAD),
        DIGEST,
        read_tag,
        read_loaded,
    )

    await guard.run_boundary("before", "case", 0)
    with pytest.raises(provenance.ReleaseProvenanceError, match="loaded.*digest"):
        await guard.run_boundary("after", "case", 0)


async def test_run_after_accepts_loaded_digest_matching_baseline(tmp_path):
    async def read_digest(_model):
        return DIGEST

    guard = provenance.ReleaseGuard(
        tmp_path,
        provenance.GitSnapshot(HEAD),
        DIGEST,
        read_digest,
        read_digest,
    )

    await guard.run_boundary("before", "case", 0)
    await guard.run_boundary("after", "case", 0)
