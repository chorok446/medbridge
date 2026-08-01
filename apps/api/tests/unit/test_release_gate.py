"""출시 게이트(check_release_gate) 회귀 테스트 — 자기참조 SHA 문제 재발 방지 포함.

repo 루트의 scripts/check_release_gate.py를 경로로 로드해 순수 코어를 검증한다.
git 연산은 주입하므로 실제 git 저장소가 필요 없다.
"""

import hashlib
import importlib.util
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[4] / "scripts" / "check_release_gate.py"
_spec = importlib.util.spec_from_file_location("check_release_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

ARTIFACT_REL = "docs/testing/qa-eval-qwen3-8b.json"


def _write_artifact(repo_root: Path, content: bytes = b'{"model":"qwen3:8b"}') -> str:
    p = repo_root / ARTIFACT_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _approval(sha256: str, **over) -> dict:
    base = {
        "testedCommit": "a" * 40,
        "qwen3_8b": {"verdict": "default_recommended"},
        "windowsValidation": {"status": "passed"},
        "artifacts": [{"path": ARTIFACT_REL, "sha256": sha256}],
    }
    base.update(over)
    return base


def _run(repo_root, approval, *, current_sha="b" * 40, commit_exists=True,
         is_ancestor=True, changed=None):
    changed = changed if changed is not None else ["docs/testing/release-approval.json"]
    gate.check_release_gate(
        approval,
        current_sha=current_sha,
        repo_root=repo_root,
        commit_exists=lambda s: commit_exists,
        is_ancestor=lambda a, b: is_ancestor,
        changed_files=lambda a, b: changed,
    )


def test_passes_even_when_current_sha_differs_from_tested(tmp_path):
    # 자기참조 수정 핵심: 승인 커밋(current) != testedCommit이어도 통과해야 한다.
    sha = _write_artifact(tmp_path)
    _run(tmp_path, _approval(sha), current_sha="b" * 40)  # tested=a.., current=b.. → 통과


def test_only_report_added_passes(tmp_path):
    sha = _write_artifact(tmp_path)
    _run(tmp_path, _approval(sha),
         changed=["docs/testing/sprint4c-qa-release-report.md", ARTIFACT_REL])


def test_not_ancestor_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(sha), is_ancestor=False)


def test_app_code_changed_since_tested_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(sha),
             changed=["apps/api/app/services/qa/schema.py"])


def test_release_workflow_changed_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(sha), changed=[".github/workflows/release.yml"])


def test_artifact_hash_mismatch_fails(tmp_path):
    _write_artifact(tmp_path, b'{"model":"qwen3:8b"}')
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval("0" * 64))  # 잘못된 해시


def test_artifact_missing_fails(tmp_path):
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval("0" * 64))  # 파일 자체가 없음


def test_path_traversal_rejected(tmp_path):
    approval = _approval("0" * 64)
    approval["artifacts"] = [{"path": "../../etc/passwd", "sha256": "0" * 64}]
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_absolute_artifact_path_rejected(tmp_path):
    approval = _approval("0" * 64)
    approval["artifacts"] = [{"path": "/etc/passwd", "sha256": "0" * 64}]
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_missing_tested_commit_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    approval = _approval(sha)
    approval.pop("testedCommit")
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_nonexistent_commit_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(sha), commit_exists=False)


def test_verdict_not_recommended_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    approval = _approval(sha, qwen3_8b={"verdict": "release_hold"})
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_windows_not_passed_fails(tmp_path):
    sha = _write_artifact(tmp_path)
    approval = _approval(sha, windowsValidation={"status": "pending"})
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)
