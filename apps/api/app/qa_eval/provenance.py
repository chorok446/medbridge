"""출시 Qwen 평가의 Git·모델 실행 정체성을 fail-closed로 고정한다."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ALLOWED_UNTRACKED = frozenset({"?? .codex/hooks.json"})

DigestReader = Callable[[str], Awaitable[str]]


class ReleaseProvenanceError(RuntimeError):
    """출시 평가 provenance를 신뢰할 수 없음."""


@dataclass(frozen=True)
class GitSnapshot:
    tested_commit: str

    def __post_init__(self) -> None:
        if _COMMIT_RE.fullmatch(self.tested_commit) is None:
            raise ReleaseProvenanceError("Git HEAD가 40자리 소문자 commit SHA가 아닙니다.")


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


def _git_stdout(repo_root: Path, *args: str) -> str:
    try:
        result = _git(repo_root, *args)
    except OSError as exc:
        raise ReleaseProvenanceError("Git 상태를 확인할 수 없습니다.") from exc
    if result.returncode != 0:
        raise ReleaseProvenanceError("Git 상태를 확인할 수 없습니다.")
    return result.stdout


def capture_git_snapshot(repo_root: str | Path) -> GitSnapshot:
    """HEAD→전체 status→HEAD를 읽어 경쟁 변경과 dirty tree를 함께 거부한다.

    `.codex/hooks.json`은 Codex 데스크톱의 작업 설정이고 evaluator·앱·Git이 import하거나
    실행하지 않는다. 현재 사용자 소유 파일을 덮어쓰지 않기 위해 이 정확한 경로 하나만
    untracked 예외로 허용하며, 다른 `.codex` 파일과 모든 코드·환경 파일은 거부한다.
    """
    root = Path(repo_root)
    head_before = _git_stdout(root, "rev-parse", "--verify", "HEAD").strip()
    status = _git_stdout(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    head_after = _git_stdout(root, "rev-parse", "--verify", "HEAD").strip()
    if head_before != head_after:
        raise ReleaseProvenanceError("Git HEAD가 snapshot 도중 변경되었습니다.")
    snapshot = GitSnapshot(head_before)
    entries = {entry for entry in status.split("\0") if entry}
    unexpected = entries - _ALLOWED_UNTRACKED
    if unexpected:
        raise ReleaseProvenanceError("출시 평가 worktree에 추적되거나 알 수 없는 변경이 있습니다.")
    return snapshot


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise ReleaseProvenanceError("Ollama 모델 digest가 정규 SHA-256 형식이 아닙니다.")
    return digest


@dataclass(frozen=True)
class ReleaseGuard:
    repo_root: Path
    snapshot: GitSnapshot
    model_digest: str
    read_digest: DigestReader
    read_loaded_digest: DigestReader
    model: str = "qwen3:8b"

    @classmethod
    async def create(
        cls,
        repo_root: str | Path,
        *,
        read_digest: DigestReader | None = None,
        read_loaded_digest: DigestReader | None = None,
        initial_snapshot: GitSnapshot | None = None,
    ) -> ReleaseGuard:
        if read_digest is None or read_loaded_digest is None:
            from app.qa_eval.ollama import loaded_release_model_digest, release_model_digest

            read_digest = read_digest or release_model_digest
            read_loaded_digest = read_loaded_digest or loaded_release_model_digest
        root = Path(repo_root)
        snapshot = initial_snapshot or capture_git_snapshot(root)
        try:
            digest = _validate_digest(await read_digest("qwen3:8b"))
        except ReleaseProvenanceError:
            raise
        except Exception as exc:
            raise ReleaseProvenanceError("Ollama 모델 digest를 확인할 수 없습니다.") from exc
        return cls(root, snapshot, digest, read_digest, read_loaded_digest)

    async def verify_digest(self, stage: str) -> None:
        try:
            current = _validate_digest(await self.read_digest(self.model))
        except ReleaseProvenanceError:
            raise
        except Exception as exc:
            raise ReleaseProvenanceError(
                f"{stage} 단계에서 Ollama 모델 digest를 확인할 수 없습니다."
            ) from exc
        if current != self.model_digest:
            raise ReleaseProvenanceError(f"{stage} 단계에서 Ollama 모델 digest가 변경되었습니다.")

    async def run_boundary(self, stage: str, case_id: str, run_index: int) -> None:
        await self.verify_digest(f"{stage}:{case_id}:{run_index}")
        if stage == "after":
            try:
                loaded = _validate_digest(await self.read_loaded_digest(self.model))
            except ReleaseProvenanceError:
                raise
            except Exception as exc:
                raise ReleaseProvenanceError(
                    f"after:{case_id}:{run_index} loaded model digest를 확인할 수 없습니다."
                ) from exc
            if loaded != self.model_digest:
                raise ReleaseProvenanceError(
                    f"after:{case_id}:{run_index} loaded model digest가 변경되었습니다."
                )

    def verify_repository(self) -> None:
        final = capture_git_snapshot(self.repo_root)
        if final.tested_commit != self.snapshot.tested_commit:
            raise ReleaseProvenanceError("출시 평가 도중 Git HEAD가 변경되었습니다.")
