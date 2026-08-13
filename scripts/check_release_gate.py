#!/usr/bin/env python3
"""출시 게이트 — main 배포 전 실제 qwen3:8b 평가·Windows 실기기 검증 완료를 강제한다.

fail-closed: 승인 파일(docs/testing/release-approval.json)이 없거나, 승인이 대상으로 한
코드 커밋(testedCommit) 이후 애플리케이션 코드가 바뀌었거나, 판정이 미완이거나, 평가
artifact 해시가 어긋나면 비정상 종료해 릴리스를 막는다.

자기참조 문제 회피: 승인 파일을 커밋하면 커밋 SHA가 바뀌므로 "승인.commit == 현재 SHA"는
정상 Git 커밋으로 충족할 수 없다. 대신 승인은 **실제로 평가·검증한 코드 커밋(testedCommit)**
을 가리키고, testedCommit이 현재 HEAD의 조상이며 그 이후 변경이 승인·보고·artifact 파일로만
한정되는지 확인한다(앱 코드·프롬프트·검색·출처 검증·평가기·release 워크플로가 바뀌면 거부).

승인 파일 형식(artifact JSON 내부 provenance도 같은 값이어야 한다):
  {
    "testedCommit": "<평가·검증을 수행한 코드 커밋 전체 SHA>",
    "qwen3_8b": {
      "verdict": "default_recommended",
      "evalArtifact": "docs/testing/qa-eval-qwen3-8b.json",
      "modelDigest": "sha256:<64자리 hex>"
    },
    "windowsValidation": {
      "status": "passed",
      "validationArtifact": "docs/testing/windows-release-validation.json",
      "installerSha256": "<64자리 hex>"
    },
    "artifacts": [
      {"kind": "qwen3_8b_evaluation", "path": "...", "sha256": "<hex>"},
      {"kind": "windows_validation", "path": "...", "sha256": "<hex>"}
    ]
  }
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPROVAL_REL = "docs/testing/release-approval.json"
_QWEN_MODEL = "qwen3:8b"
_QWEN_ARTIFACT_KIND = "qwen3_8b_evaluation"
_WINDOWS_ARTIFACT_KIND = "windows_validation"
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

# testedCommit 이후 변경이 허용되는 비-코드 파일(승인·보고·검증 문서). artifact 경로는
# 승인 파일에 명시된 것만 추가로 허용한다.
_ALLOWED_CHANGED = frozenset(
    {
        "docs/testing/release-approval.json",
        "docs/testing/sprint4c-qa-release-report.md",
        "docs/testing/windows-sprint4c-qa-release-validation.md",
    }
)


class GateError(Exception):
    """게이트 거부 사유(사용자 표시용)."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_full_sha(value: object, *, field: str) -> str:
    sha = str(value).strip()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise GateError(f"{field}은 40자리 소문자 Git SHA여야 합니다.")
    return sha


def _require_sha256(value: object, *, field: str) -> str:
    digest = str(value).strip()
    if not _SHA256_RE.fullmatch(digest):
        raise GateError(f"{field}은 64자리 소문자 SHA-256이어야 합니다.")
    return digest


def _require_model_digest(value: object, *, field: str) -> str:
    digest = str(value).strip()
    if not _MODEL_DIGEST_RE.fullmatch(digest):
        raise GateError(f"{field}은 sha256: 접두사가 있는 64자리 소문자 digest여야 합니다.")
    return digest


def _load_json_artifact(path: Path, *, rel: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise GateError(f"artifact JSON을 읽을 수 없습니다: {rel} ({type(exc).__name__})") from exc
    if not isinstance(payload, dict):
        raise GateError(f"artifact JSON 최상위 값은 매핑이어야 합니다: {rel}")
    return payload


def _require_same(actual: object, expected: object, *, field: str) -> None:
    if actual != expected:
        raise GateError(f"artifact의 {field}가 승인 파일과 일치하지 않습니다.")


def _safe_repo_path(repo_root: Path, rel: str) -> Path:
    """저장소 안의 상대 경로만 허용한다(경로 순회·절대경로·저장소 밖 참조 거부)."""
    if not rel or rel.startswith(("/", "\\")):
        raise GateError(f"artifact 경로가 상대경로가 아닙니다: {rel}")
    if ".." in Path(rel).parts:
        raise GateError(f"artifact 경로에 상위 참조(..)가 있습니다: {rel}")
    resolved = (repo_root / rel).resolve()
    root = repo_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise GateError(f"artifact가 저장소 밖을 가리킵니다: {rel}")
    return resolved


def check_release_gate(
    approval: dict,
    *,
    current_sha: str,
    repo_root: Path,
    commit_exists: Callable[[str], bool],
    is_ancestor: Callable[[str, str], bool],
    changed_files: Callable[[str, str], list[str]],
    installer_path: Path | None = None,
) -> None:
    """게이트 통과면 정상 반환, 아니면 GateError. git 연산은 주입받아 테스트 가능하게 한다."""
    if not isinstance(approval, dict):
        raise GateError("승인 파일 최상위 값은 매핑이어야 합니다.")
    # 현재 커밋을 알 수 없으면 조상·변경 파일 검사를 할 수 없다 → fail-closed.
    current = _require_full_sha(current_sha, field="현재 커밋 SHA")
    tested = _require_full_sha(approval.get("testedCommit", ""), field="testedCommit")
    if not commit_exists(tested):
        raise GateError(f"testedCommit이 저장소에 존재하지 않습니다: {tested[:12]}")
    if not is_ancestor(tested, current):
        raise GateError(
            f"testedCommit({tested[:12]})이 현재 커밋({current[:12]})의 조상이 아닙니다."
        )

    # artifact: 승인에 기록된 경로만 추가 허용 + 파일 해시 + JSON provenance 확인.
    # 빈 리스트를 falsy default로 흘려보내지 않고 필수 두 종류가 모두 있는지 강제한다.
    artifacts = approval.get("artifacts")
    if not isinstance(artifacts, list):
        raise GateError("artifacts는 리스트여야 합니다.")
    if not artifacts:
        raise GateError("artifacts가 비어 있습니다. Qwen 평가와 Windows 검증 JSON이 필요합니다.")
    allowed_changed = set(_ALLOWED_CHANGED)
    artifact_by_kind: dict[str, tuple[str, dict]] = {}
    seen_paths: set[str] = set()
    for art in artifacts:
        if not isinstance(art, dict):
            raise GateError("각 artifact는 매핑이어야 합니다.")
        rel = str(art.get("path", "")).strip()
        if rel in seen_paths:
            raise GateError(f"artifact 경로가 중복되었습니다: {rel}")
        seen_paths.add(rel)
        expected = _require_sha256(art.get("sha256", ""), field=f"artifact sha256({rel})")
        target = _safe_repo_path(repo_root, rel)  # 경로 순회 거부
        if not rel.startswith("docs/testing/"):
            raise GateError(f"artifact는 docs/testing/ 아래에 있어야 합니다: {rel}")
        if not target.is_file():
            raise GateError(f"artifact 파일이 없습니다: {rel}")
        actual = _sha256_file(target)
        if actual != expected:
            raise GateError(f"artifact 해시 불일치: {rel}")
        allowed_changed.add(rel)

        kind = str(art.get("kind", "")).strip()
        if kind in {_QWEN_ARTIFACT_KIND, _WINDOWS_ARTIFACT_KIND}:
            if kind in artifact_by_kind:
                raise GateError(f"필수 artifact 종류가 중복되었습니다: {kind}")
            artifact_by_kind[kind] = (rel, _load_json_artifact(target, rel=rel))

    missing_kinds = [
        kind
        for kind in (_QWEN_ARTIFACT_KIND, _WINDOWS_ARTIFACT_KIND)
        if kind not in artifact_by_kind
    ]
    if missing_kinds:
        raise GateError("필수 artifact가 없습니다: " + ", ".join(missing_kinds))

    qwen = approval.get("qwen3_8b")
    if not isinstance(qwen, dict):
        raise GateError("qwen3_8b 승인은 매핑이어야 합니다.")
    if qwen.get("verdict") != "default_recommended":
        raise GateError(f"qwen3:8b 판정이 default_recommended가 아닙니다: {qwen.get('verdict')}")
    model_digest = _require_model_digest(qwen.get("modelDigest", ""), field="qwen3_8b.modelDigest")
    qwen_rel, qwen_payload = artifact_by_kind[_QWEN_ARTIFACT_KIND]
    _require_same(qwen.get("evalArtifact"), qwen_rel, field="qwen3_8b.evalArtifact")
    _require_same(qwen_payload.get("artifactType"), _QWEN_ARTIFACT_KIND, field="Qwen artifactType")
    _require_same(qwen_payload.get("schemaVersion"), 1, field="Qwen schemaVersion")
    _require_same(qwen_payload.get("testedCommit"), tested, field="Qwen testedCommit")
    _require_same(qwen_payload.get("provider"), "local", field="Qwen provider")
    _require_same(qwen_payload.get("model"), _QWEN_MODEL, field="Qwen model")
    _require_same(qwen_payload.get("modelDigest"), model_digest, field="Qwen modelDigest")
    qwen_gate = qwen_payload.get("gate")
    if not isinstance(qwen_gate, dict):
        raise GateError("Qwen artifact의 gate가 매핑이 아닙니다.")
    _require_same(qwen_gate.get("verdict"), "default_recommended", field="Qwen gate.verdict")
    for field in ("safetyPassed", "explicitSafetyPassed", "criticalCasesPassed", "modelGatePassed"):
        _require_same(qwen_gate.get(field), True, field=f"Qwen gate.{field}")
    cases = qwen_payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise GateError("Qwen artifact에 평가 cases가 없습니다.")
    if any(
        not isinstance(case, dict)
        or not isinstance(case.get("runs"), int)
        or isinstance(case.get("runs"), bool)
        or case["runs"] < 3
        for case in cases
    ):
        raise GateError("Qwen 출시 평가는 모든 case를 최소 3회 실행해야 합니다.")

    win = approval.get("windowsValidation")
    if not isinstance(win, dict):
        raise GateError("windowsValidation 승인은 매핑이어야 합니다.")
    if win.get("status") != "passed":
        raise GateError(f"Windows 실기기 검증이 passed가 아닙니다: {win.get('status')}")
    installer_sha256 = _require_sha256(
        win.get("installerSha256", ""), field="windowsValidation.installerSha256"
    )
    win_rel, win_payload = artifact_by_kind[_WINDOWS_ARTIFACT_KIND]
    _require_same(
        win.get("validationArtifact"), win_rel, field="windowsValidation.validationArtifact"
    )
    _require_same(
        win_payload.get("artifactType"), _WINDOWS_ARTIFACT_KIND, field="Windows artifactType"
    )
    _require_same(win_payload.get("schemaVersion"), 1, field="Windows schemaVersion")
    _require_same(win_payload.get("testedCommit"), tested, field="Windows testedCommit")
    _require_same(win_payload.get("status"), "passed", field="Windows status")
    _require_same(win_payload.get("model"), _QWEN_MODEL, field="Windows model")
    _require_same(win_payload.get("modelDigest"), model_digest, field="Windows modelDigest")
    _require_same(
        win_payload.get("installerSha256"), installer_sha256, field="Windows installerSha256"
    )
    checklist = win_payload.get("checklist")
    if not isinstance(checklist, dict):
        raise GateError("Windows artifact의 checklist가 매핑이 아닙니다.")
    for field, expected in (("total", 36), ("passed", 36), ("failed", 0), ("blocked", 0)):
        _require_same(checklist.get(field), expected, field=f"Windows checklist.{field}")

    # 릴리스 워크플로가 새로 만든 installer를 제공하면 실기기에서 검증한 바로 그
    # 바이너리인지 확인한다. 재빌드 결과가 한 바이트라도 다르면 공개 게시를 막는다.
    if installer_path is not None:
        if not installer_path.is_file():
            raise GateError(f"검증할 installer 파일이 없습니다: {installer_path}")
        if _sha256_file(installer_path) != installer_sha256:
            raise GateError(
                "빌드된 installer가 Windows 실기기 검증 대상과 다릅니다(SHA-256 불일치)."
            )

    # testedCommit 이후 변경은 승인·보고·artifact 파일로만 한정되어야 한다.
    changed = changed_files(tested, current)
    unexpected = sorted(c for c in changed if c not in allowed_changed)
    if unexpected:
        raise GateError(
            "testedCommit 이후 승인·보고·artifact 외 파일이 변경되었습니다(재평가 필요): "
            + ", ".join(unexpected[:10])
        )


# --- 실제 git 연산 (main에서 주입) ---
def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_exists(repo_root: Path) -> Callable[[str], bool]:
    def _inner(sha: str) -> bool:
        return _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0

    return _inner


def _is_ancestor(repo_root: Path) -> Callable[[str, str], bool]:
    def _inner(ancestor: str, descendant: str) -> bool:
        return _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    return _inner


def _changed_files(repo_root: Path) -> Callable[[str, str], list[str]]:
    def _inner(base: str, head: str) -> list[str]:
        res = _git(repo_root, "diff", "--name-only", base, head)
        if res.returncode != 0:
            raise GateError(f"git diff 실패: {res.stderr.strip()[:200]}")
        return [line for line in res.stdout.splitlines() if line.strip()]

    return _inner


def main() -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="MedBridge 출시 승인 artifact 검증")
    parser.add_argument(
        "--installer",
        type=Path,
        default=None,
        help="빌드된 Windows installer 경로(실기기 검증 SHA-256과 일치해야 함)",
    )
    args = parser.parse_args()

    current_sha = os.environ.get("GITHUB_SHA", "").strip()
    if not current_sha:
        res = _git(_REPO_ROOT, "rev-parse", "HEAD")
        current_sha = res.stdout.strip() if res.returncode == 0 else ""

    approval_path = _REPO_ROOT / _APPROVAL_REL
    if not approval_path.exists():
        print("::error::출시 게이트 실패 — release-approval.json이 없습니다(출시 보류 상태).")
        _print_howto()
        return 1
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"::error::출시 게이트 실패 — 승인 파일을 읽을 수 없습니다: {type(exc).__name__}")
        return 1

    try:
        check_release_gate(
            approval,
            current_sha=current_sha,
            repo_root=_REPO_ROOT,
            commit_exists=_commit_exists(_REPO_ROOT),
            is_ancestor=_is_ancestor(_REPO_ROOT),
            changed_files=_changed_files(_REPO_ROOT),
            installer_path=args.installer,
        )
    except GateError as exc:
        print(f"::error::출시 게이트 실패 — {exc}")
        _print_howto()
        return 1

    installer_message = ", installer 해시 일치" if args.installer is not None else ""
    print(
        "출시 게이트 통과 — qwen3:8b default_recommended + Windows passed, "
        f"provenance 교차검증 완료, artifact 해시 일치{installer_message}."
    )
    return 0


def _print_howto() -> None:
    print(
        "실제 qwen3:8b 평가(default_recommended)와 Windows 실기기 검증(passed)을 수행한 "
        "코드 커밋을 testedCommit으로 하여 docs/testing/release-approval.json을 작성하고, "
        "그 이후에는 승인·보고·artifact 파일만 커밋하세요."
    )


if __name__ == "__main__":
    raise SystemExit(main())
