"""출시 게이트(check_release_gate) provenance·fail-closed 회귀 테스트."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[4] / "scripts" / "check_release_gate.py"
_RELEASE_WORKFLOW_PATH = Path(__file__).resolve().parents[4] / ".github/workflows/release.yml"
_spec = importlib.util.spec_from_file_location("check_release_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

TESTED = "a" * 40
CURRENT = "b" * 40
MODEL_DIGEST = "sha256:" + "c" * 64
INSTALLER_SHA256 = "d" * 64
QWEN_REL = "docs/testing/qa-eval-qwen3-8b.json"
WINDOWS_REL = "docs/testing/windows-release-validation.json"


def _json_artifact(repo_root: Path, rel: str, kind: str, payload: dict) -> dict:
    content = json.dumps(payload, sort_keys=True).encode()
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"kind": kind, "path": rel, "sha256": hashlib.sha256(content).hexdigest()}


def _approval(repo_root: Path, **over) -> dict:
    qwen_payload = {
        "artifactType": "qwen3_8b_evaluation",
        "schemaVersion": 1,
        "testedCommit": TESTED,
        "provider": "local",
        "model": "qwen3:8b",
        "modelDigest": MODEL_DIGEST,
        "gate": {
            "verdict": "default_recommended",
            "safetyPassed": True,
            "explicitSafetyPassed": True,
            "criticalCasesPassed": True,
            "modelGatePassed": True,
        },
        "cases": [{"caseId": "grounded", "runs": 3}],
    }
    windows_payload = {
        "artifactType": "windows_validation",
        "schemaVersion": 1,
        "testedCommit": TESTED,
        "status": "passed",
        "model": "qwen3:8b",
        "modelDigest": MODEL_DIGEST,
        "installerSha256": INSTALLER_SHA256,
        "checklist": {"total": 36, "passed": 36, "failed": 0, "blocked": 0},
    }
    artifacts = [
        _json_artifact(repo_root, QWEN_REL, "qwen3_8b_evaluation", qwen_payload),
        _json_artifact(repo_root, WINDOWS_REL, "windows_validation", windows_payload),
    ]
    base = {
        "testedCommit": TESTED,
        "qwen3_8b": {
            "verdict": "default_recommended",
            "evalArtifact": QWEN_REL,
            "modelDigest": MODEL_DIGEST,
        },
        "windowsValidation": {
            "status": "passed",
            "validationArtifact": WINDOWS_REL,
            "installerSha256": INSTALLER_SHA256,
        },
        "artifacts": artifacts,
    }
    base.update(over)
    return base


def _replace_artifact(repo_root: Path, approval: dict, kind: str, **changes) -> None:
    artifact = next(item for item in approval["artifacts"] if item["kind"] == kind)
    path = repo_root / artifact["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    content = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(content)
    artifact["sha256"] = hashlib.sha256(content).hexdigest()


def _run(
    repo_root: Path,
    approval: dict,
    *,
    current_sha: str = CURRENT,
    commit_exists: bool = True,
    is_ancestor: bool = True,
    changed: list[str] | None = None,
    installer_path: Path | None = None,
) -> None:
    changed = changed if changed is not None else ["docs/testing/release-approval.json"]
    gate.check_release_gate(
        approval,
        current_sha=current_sha,
        repo_root=repo_root,
        commit_exists=lambda _: commit_exists,
        is_ancestor=lambda _a, _b: is_ancestor,
        changed_files=lambda _a, _b: changed,
        installer_path=installer_path,
    )


def test_passes_even_when_current_sha_differs_from_tested(tmp_path):
    _run(tmp_path, _approval(tmp_path))


def test_non_mapping_approval_fails_closed(tmp_path):
    with pytest.raises(gate.GateError, match="최상위"):
        _run(tmp_path, [])


def test_workflow_dispatch_rejects_non_main_ref_before_checkout():
    workflow = _RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    guard = 'if [ "$RELEASE_REF" != "refs/heads/main" ]; then'
    assert "workflow_dispatch:" in workflow
    assert "RELEASE_REF: ${{ github.ref }}" in workflow
    assert guard in workflow
    assert workflow.index(guard) < workflow.index("actions/checkout@")
    guard_body = workflow[workflow.index(guard) : workflow.index("actions/checkout@")]
    assert "exit 1" in guard_body


def test_only_report_and_declared_artifacts_added_passes(tmp_path):
    _run(
        tmp_path,
        _approval(tmp_path),
        changed=["docs/testing/sprint4c-qa-release-report.md", QWEN_REL, WINDOWS_REL],
    )


@pytest.mark.parametrize("tested", ["HEAD", "A" * 40, "a" * 39, "a" * 41, "g" * 40])
def test_tested_commit_must_be_full_lowercase_sha(tmp_path, tested):
    approval = _approval(tmp_path)
    approval["testedCommit"] = tested
    with pytest.raises(gate.GateError, match="40자리 소문자"):
        _run(tmp_path, approval)


def test_missing_tested_commit_fails(tmp_path):
    approval = _approval(tmp_path)
    approval.pop("testedCommit")
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


@pytest.mark.parametrize("current", ["", "HEAD", "B" * 40])
def test_invalid_current_sha_fails_closed(tmp_path, current):
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(tmp_path), current_sha=current)


def test_nonexistent_commit_fails(tmp_path):
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(tmp_path), commit_exists=False)


def test_not_ancestor_fails(tmp_path):
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(tmp_path), is_ancestor=False)


@pytest.mark.parametrize(
    "changed",
    [["apps/api/app/services/qa/schema.py"], [".github/workflows/release.yml"]],
)
def test_code_or_release_workflow_changed_since_tested_fails(tmp_path, changed):
    with pytest.raises(gate.GateError):
        _run(tmp_path, _approval(tmp_path), changed=changed)


def test_empty_artifacts_fails_closed(tmp_path):
    with pytest.raises(gate.GateError, match="비어"):
        _run(tmp_path, _approval(tmp_path, artifacts=[]))


@pytest.mark.parametrize("missing_kind", ["qwen3_8b_evaluation", "windows_validation"])
def test_both_required_artifact_kinds_are_mandatory(tmp_path, missing_kind):
    approval = _approval(tmp_path)
    approval["artifacts"] = [a for a in approval["artifacts"] if a["kind"] != missing_kind]
    with pytest.raises(gate.GateError, match="필수 artifact"):
        _run(tmp_path, approval)


def test_artifact_hash_mismatch_fails(tmp_path):
    approval = _approval(tmp_path)
    approval["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(gate.GateError, match="해시 불일치"):
        _run(tmp_path, approval)


def test_artifact_hash_must_be_lowercase_full_sha256(tmp_path):
    approval = _approval(tmp_path)
    approval["artifacts"][0]["sha256"] = "A" * 64
    with pytest.raises(gate.GateError, match="64자리 소문자"):
        _run(tmp_path, approval)


def test_artifact_missing_fails(tmp_path):
    approval = _approval(tmp_path)
    (tmp_path / QWEN_REL).unlink()
    with pytest.raises(gate.GateError, match="파일이 없습니다"):
        _run(tmp_path, approval)


@pytest.mark.parametrize("bad_path", ["../../etc/passwd", "/etc/passwd"])
def test_unsafe_artifact_path_rejected(tmp_path, bad_path):
    approval = _approval(tmp_path)
    approval["artifacts"][0]["path"] = bad_path
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_artifact_cannot_allow_changed_application_code(tmp_path):
    approval = _approval(tmp_path)
    app_file = tmp_path / "apps/api/app/main.py"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("changed", encoding="utf-8")
    approval["artifacts"].append(
        {
            "path": "apps/api/app/main.py",
            "sha256": hashlib.sha256(b"changed").hexdigest(),
        }
    )
    with pytest.raises(gate.GateError, match="docs/testing"):
        _run(tmp_path, approval, changed=["apps/api/app/main.py"])


def test_historical_qwen_artifact_commit_mismatch_fails(tmp_path):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, "qwen3_8b_evaluation", testedCommit="9" * 40)
    with pytest.raises(gate.GateError, match="Qwen testedCommit"):
        _run(tmp_path, approval)


def test_historical_windows_artifact_commit_mismatch_fails(tmp_path):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, "windows_validation", testedCommit="9" * 40)
    with pytest.raises(gate.GateError, match="Windows testedCommit"):
        _run(tmp_path, approval)


@pytest.mark.parametrize("kind", ["qwen3_8b_evaluation", "windows_validation"])
def test_artifact_model_mismatch_fails(tmp_path, kind):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, kind, model="qwen3:4b")
    with pytest.raises(gate.GateError, match="model"):
        _run(tmp_path, approval)


@pytest.mark.parametrize("kind", ["qwen3_8b_evaluation", "windows_validation"])
def test_artifact_model_digest_mismatch_fails(tmp_path, kind):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, kind, modelDigest="sha256:" + "e" * 64)
    with pytest.raises(gate.GateError, match="modelDigest"):
        _run(tmp_path, approval)


def test_windows_installer_hash_mismatch_fails(tmp_path):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, "windows_validation", installerSha256="e" * 64)
    with pytest.raises(gate.GateError, match="installerSha256"):
        _run(tmp_path, approval)


def test_verdict_not_recommended_fails(tmp_path):
    approval = _approval(tmp_path)
    approval["qwen3_8b"]["verdict"] = "release_hold"
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_qwen_artifact_failed_gate_fails(tmp_path):
    approval = _approval(tmp_path)
    path = tmp_path / QWEN_REL
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["gate"]["safetyPassed"] = False
    content = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(content)
    approval["artifacts"][0]["sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(gate.GateError, match="safetyPassed"):
        _run(tmp_path, approval)


def test_qwen_artifact_requires_three_runs_per_case(tmp_path):
    approval = _approval(tmp_path)
    _replace_artifact(tmp_path, approval, "qwen3_8b_evaluation", cases=[{"runs": 2}])
    with pytest.raises(gate.GateError, match="최소 3회"):
        _run(tmp_path, approval)


def test_windows_not_passed_fails(tmp_path):
    approval = _approval(tmp_path)
    approval["windowsValidation"]["status"] = "pending"
    with pytest.raises(gate.GateError):
        _run(tmp_path, approval)


def test_windows_checklist_must_have_all_36_passed(tmp_path):
    approval = _approval(tmp_path)
    _replace_artifact(
        tmp_path,
        approval,
        "windows_validation",
        checklist={"total": 36, "passed": 35, "failed": 1, "blocked": 0},
    )
    with pytest.raises(gate.GateError, match="checklist.passed"):
        _run(tmp_path, approval)


def test_built_installer_must_match_validated_sha256(tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"verified installer")
    installer_sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    approval = _approval(tmp_path)
    approval["windowsValidation"]["installerSha256"] = installer_sha
    _replace_artifact(tmp_path, approval, "windows_validation", installerSha256=installer_sha)
    _run(tmp_path, approval, installer_path=installer)

    installer.write_bytes(b"different rebuild")
    with pytest.raises(gate.GateError, match="installer.*불일치"):
        _run(tmp_path, approval, installer_path=installer)
