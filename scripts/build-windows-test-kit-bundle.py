#!/usr/bin/env python3
"""NSIS installer와 합성 PDF를 provenance가 고정된 실기기 검증 bundle로 묶는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_PDFS = (
    "01_digital_simple.pdf",
    "02_two_column_paper.pdf",
    "03_table_doc.pdf",
    "04_rotated_page.pdf",
    "05_scanned_korean_short.pdf",
    "06_scanned_korean_long.pdf",
    "07_mixed_50pages.pdf",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle(
    *,
    installer: Path,
    kit_dir: Path,
    output_dir: Path,
    tested_commit: str,
    workflow_run_id: int,
) -> dict:
    """검증 입력을 복사하고 hash manifest를 반환한다. 기존 파일은 덮어쓰지 않는다."""
    if not _COMMIT_RE.fullmatch(tested_commit):
        raise ValueError("tested commit must be a 40-character lowercase SHA")
    if workflow_run_id <= 0:
        raise ValueError("workflow run id must be positive")
    if not installer.is_file() or installer.suffix.lower() != ".exe":
        raise ValueError("exactly one existing NSIS installer is required")

    pdfs = [kit_dir / name for name in EXPECTED_PDFS]
    missing = [path.name for path in pdfs if not path.is_file()]
    extras = sorted(path.name for path in kit_dir.glob("*.pdf") if path.name not in EXPECTED_PDFS)
    if missing or extras:
        raise ValueError(f"test kit mismatch: missing={missing}, extras={extras}")

    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty bundle: {output_dir}")
    bundle_kit_dir = output_dir / "windows-test-kit"
    bundle_kit_dir.mkdir(parents=True, exist_ok=True)

    installer_target = output_dir / installer.name
    shutil.copy2(installer, installer_target)
    for path in pdfs:
        shutil.copy2(path, bundle_kit_dir / path.name)

    manifest = {
        "schemaVersion": 1,
        "testedCommit": tested_commit,
        "workflowRunId": workflow_run_id,
        "installer": {
            "name": installer_target.name,
            "sha256": sha256_file(installer_target),
            "size": installer_target.stat().st_size,
        },
        "testKit": [
            {
                "name": path.name,
                "sha256": sha256_file(bundle_kit_dir / path.name),
                "size": (bundle_kit_dir / path.name).stat().st_size,
            }
            for path in pdfs
        ],
    }
    (bundle_kit_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--kit-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    args = parser.parse_args()
    build_bundle(
        installer=args.installer,
        kit_dir=args.kit_dir,
        output_dir=args.output,
        tested_commit=args.tested_commit,
        workflow_run_id=args.workflow_run_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
