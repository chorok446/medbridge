"""Windows 실기기 bundle의 commit/installer/PDF provenance 회귀 테스트."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "build-windows-test-kit-bundle.py"
_spec = importlib.util.spec_from_file_location("build_windows_test_kit_bundle", _SCRIPT)
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)


def _inputs(root: Path) -> tuple[Path, Path]:
    installer = root / "MedBridge-setup.exe"
    installer.write_bytes(b"installer")
    kit = root / "kit"
    kit.mkdir()
    for index, name in enumerate(bundle.EXPECTED_PDFS):
        (kit / name).write_bytes(f"pdf-{index}".encode())
    return installer, kit


def test_bundle_pins_commit_installer_and_all_expected_pdfs(tmp_path: Path) -> None:
    installer, kit = _inputs(tmp_path)
    output = tmp_path / "bundle"

    manifest = bundle.build_bundle(
        installer=installer,
        kit_dir=kit,
        output_dir=output,
        tested_commit="a" * 40,
        workflow_run_id=123,
    )

    saved = json.loads((output / "windows-test-kit" / "manifest.json").read_text("utf-8"))
    assert saved == manifest
    assert saved["testedCommit"] == "a" * 40
    assert saved["installer"]["sha256"] == hashlib.sha256(b"installer").hexdigest()
    assert [item["name"] for item in saved["testKit"]] == list(bundle.EXPECTED_PDFS)
    assert (output / installer.name).read_bytes() == b"installer"


@pytest.mark.parametrize("commit", ["HEAD", "A" * 40, "a" * 39, "a" * 41])
def test_bundle_rejects_non_exact_commit(commit: str, tmp_path: Path) -> None:
    installer, kit = _inputs(tmp_path)
    with pytest.raises(ValueError, match="40-character lowercase SHA"):
        bundle.build_bundle(
            installer=installer,
            kit_dir=kit,
            output_dir=tmp_path / "bundle",
            tested_commit=commit,
            workflow_run_id=123,
        )


def test_bundle_rejects_missing_or_extra_pdf(tmp_path: Path) -> None:
    installer, kit = _inputs(tmp_path)
    (kit / bundle.EXPECTED_PDFS[-1]).unlink()
    (kit / "unexpected.pdf").write_bytes(b"extra")
    with pytest.raises(ValueError, match="test kit mismatch"):
        bundle.build_bundle(
            installer=installer,
            kit_dir=kit,
            output_dir=tmp_path / "bundle",
            tested_commit="b" * 40,
            workflow_run_id=123,
        )


def test_bundle_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    installer, kit = _inputs(tmp_path)
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        bundle.build_bundle(
            installer=installer,
            kit_dir=kit,
            output_dir=output,
            tested_commit="c" * 40,
            workflow_run_id=123,
        )
