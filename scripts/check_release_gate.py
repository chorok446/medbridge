#!/usr/bin/env python3
"""출시 게이트 — main 배포 전 실제 qwen3:8b 평가·Windows 실기기 검증 완료를 강제한다.

이 스크립트는 fail-closed다: 승인 파일(docs/testing/release-approval.json)이 없거나,
현재 커밋에 바인딩되지 않았거나, 판정이 완료되지 않았으면 비정상 종료해 릴리스를 막는다.
문서의 "HOLD"만으로는 발행을 막지 못하므로 워크플로에서 이 검사를 필수 단계로 둔다.

승인 파일 형식(실제 평가·검증 완료 후 커밋):
  {
    "commit": "<이 승인이 대상으로 하는 전체 SHA>",
    "qwen3_8b": {"verdict": "default_recommended", "evalArtifact": "..."},
    "windowsValidation": {"status": "passed", "date": "YYYY-MM-DD", "by": "..."}
  }
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPROVAL = _REPO_ROOT / "docs" / "testing" / "release-approval.json"


def _fail(msg: str) -> int:
    print(f"::error::출시 게이트 실패 — {msg}")
    print("실제 qwen3:8b 평가(default_recommended)와 Windows 실기기 검증(passed)을 완료하고 "
          "docs/testing/release-approval.json을 현재 커밋 SHA로 갱신해 커밋하세요.")
    return 1


def main() -> int:
    current_sha = os.environ.get("GITHUB_SHA", "").strip()
    if not _APPROVAL.exists():
        return _fail("release-approval.json이 없습니다(출시 보류 상태).")
    try:
        data = json.loads(_APPROVAL.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return _fail(f"release-approval.json을 읽을 수 없습니다: {type(exc).__name__}")

    approved_commit = str(data.get("commit", "")).strip()
    if not approved_commit:
        return _fail("승인에 commit SHA가 없습니다.")
    # 커밋 바인딩 — 다른 커밋의 오래된 승인으로 통과하지 못하게 한다.
    if current_sha and approved_commit != current_sha:
        return _fail(f"승인이 현재 커밋에 바인딩되지 않았습니다(승인 {approved_commit[:12]} "
                     f"≠ 현재 {current_sha[:12]}).")

    qwen = data.get("qwen3_8b") or {}
    if qwen.get("verdict") != "default_recommended":
        return _fail(f"qwen3:8b 판정이 default_recommended가 아닙니다: {qwen.get('verdict')}")

    win = data.get("windowsValidation") or {}
    if win.get("status") != "passed":
        return _fail(f"Windows 실기기 검증이 passed가 아닙니다: {win.get('status')}")

    print("출시 게이트 통과 — qwen3:8b default_recommended + Windows 검증 passed (커밋 바인딩).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
