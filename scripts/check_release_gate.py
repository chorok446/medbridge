#!/usr/bin/env python3
"""출시 게이트 — main 배포 전 실제 qwen3:8b 평가·Windows 실기기 검증 완료를 강제한다.

fail-closed: 승인 파일(docs/testing/release-approval.json)이 없거나, 승인이 대상으로 한
코드 커밋(testedCommit) 이후 애플리케이션 코드가 바뀌었거나, 판정이 미완이거나, 평가
artifact 해시가 어긋나면 비정상 종료해 릴리스를 막는다.

자기참조 문제 회피: 승인 파일을 커밋하면 커밋 SHA가 바뀌므로 "승인.commit == 현재 SHA"는
정상 Git 커밋으로 충족할 수 없다. 대신 승인은 **실제로 평가·검증한 코드 커밋(testedCommit)**
을 가리키고, testedCommit이 현재 HEAD의 조상이며 그 이후 변경이 승인·보고·artifact 파일로만
한정되는지 확인한다(앱 코드·프롬프트·검색·출처 검증·평가기·release 워크플로가 바뀌면 거부).

승인 파일 형식:
  {
    "testedCommit": "<평가·검증을 수행한 코드 커밋 전체 SHA>",
    "qwen3_8b": {"verdict": "default_recommended", "evalArtifact": "..."},
    "windowsValidation": {"status": "passed", "date": "YYYY-MM-DD", "by": "..."},
    "artifacts": [{"path": "docs/testing/qa-eval-qwen3-8b.json", "sha256": "<hex>"}]
  }
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPROVAL_REL = "docs/testing/release-approval.json"

# testedCommit 이후 변경이 허용되는 비-코드 파일(승인·보고·검증 문서). artifact 경로는
# 승인 파일에 명시된 것만 추가로 허용한다.
_ALLOWED_CHANGED = frozenset({
    "docs/testing/release-approval.json",
    "docs/testing/sprint4c-qa-release-report.md",
    "docs/testing/windows-sprint4c-qa-release-validation.md",
})


class GateError(Exception):
    """게이트 거부 사유(사용자 표시용)."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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
) -> None:
    """게이트 통과면 정상 반환, 아니면 GateError. git 연산은 주입받아 테스트 가능하게 한다."""
    # 현재 커밋을 알 수 없으면 조상·변경 파일 검사를 할 수 없다 → fail-closed.
    if not current_sha:
        raise GateError("현재 커밋 SHA를 확인할 수 없습니다(GITHUB_SHA/HEAD 미확인).")
    tested = str(approval.get("testedCommit", "")).strip()
    if not tested:
        raise GateError("승인에 testedCommit이 없습니다.")
    if not commit_exists(tested):
        raise GateError(f"testedCommit이 저장소에 존재하지 않습니다: {tested[:12]}")
    if not is_ancestor(tested, current_sha):
        raise GateError(
            f"testedCommit({tested[:12]})이 현재 커밋({current_sha[:12]})의 조상이 아닙니다."
        )

    # artifact: 승인에 기록된 경로만 추가 허용 + 해시 일치 확인
    artifacts = approval.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise GateError("artifacts는 리스트여야 합니다.")
    allowed_changed = set(_ALLOWED_CHANGED)
    for art in artifacts:
        if not isinstance(art, dict):
            raise GateError("각 artifact는 매핑이어야 합니다.")
        rel = str(art.get("path", ""))
        expected = str(art.get("sha256", "")).lower()
        if not expected:
            raise GateError(f"artifact sha256이 없습니다: {rel}")
        target = _safe_repo_path(repo_root, rel)  # 경로 순회 거부
        if not target.is_file():
            raise GateError(f"artifact 파일이 없습니다: {rel}")
        actual = _sha256_file(target)
        if actual != expected:
            raise GateError(f"artifact 해시 불일치: {rel}")
        allowed_changed.add(rel)

    # testedCommit 이후 변경은 승인·보고·artifact 파일로만 한정되어야 한다.
    changed = changed_files(tested, current_sha)
    unexpected = sorted(c for c in changed if c not in allowed_changed)
    if unexpected:
        raise GateError(
            "testedCommit 이후 승인·보고·artifact 외 파일이 변경되었습니다(재평가 필요): "
            + ", ".join(unexpected[:10])
        )

    qwen = approval.get("qwen3_8b") or {}
    if qwen.get("verdict") != "default_recommended":
        raise GateError(f"qwen3:8b 판정이 default_recommended가 아닙니다: {qwen.get('verdict')}")

    win = approval.get("windowsValidation") or {}
    if win.get("status") != "passed":
        raise GateError(f"Windows 실기기 검증이 passed가 아닙니다: {win.get('status')}")


# --- 실제 git 연산 (main에서 주입) ---
def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=False,
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
    import os

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
        )
    except GateError as exc:
        print(f"::error::출시 게이트 실패 — {exc}")
        _print_howto()
        return 1

    print("출시 게이트 통과 — qwen3:8b default_recommended + Windows passed, "
          "testedCommit 이후 코드 변경 없음, artifact 해시 일치.")
    return 0


def _print_howto() -> None:
    print("실제 qwen3:8b 평가(default_recommended)와 Windows 실기기 검증(passed)을 수행한 "
          "코드 커밋을 testedCommit으로 하여 docs/testing/release-approval.json을 작성하고, "
          "그 이후에는 승인·보고·artifact 파일만 커밋하세요.")


if __name__ == "__main__":
    raise SystemExit(main())
