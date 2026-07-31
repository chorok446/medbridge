#!/usr/bin/env python3
"""Windows OCR 리소스 준비 — Tesseract 5 + kor/eng/osd tessdata (버전·checksum 고정).

GitHub Actions windows-latest에서 실행한다. 산출물:
  apps/desktop/src-tauri/resources/ocr/
    tesseract.exe + DLL들 + tessdata/{kor,eng,osd}.traineddata + manifest.json

출처·라이선스: docs/licenses/ocr-components.md
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp1252)에서 한글 출력 보장

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "apps/desktop/src-tauri/resources/ocr"

# 버전 고정 + sha256 (UB-Mannheim Tesseract Windows 빌드, tessdata_fast 커밋 고정)
TESSERACT_VERSION = "5.4.0.20240606"
TESSERACT_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/"
    f"v{TESSERACT_VERSION}/tesseract-ocr-w64-setup-{TESSERACT_VERSION}.exe"
)
TESSERACT_SHA256 = "c885fff6998e0608ba4bb8ab51436e1c6775c2bafc2559a19b423e18678b60c9"

TESSDATA_COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"  # tessdata_fast 고정 커밋
TESSDATA = {
    "kor.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/kor.traineddata",
        "6b85e11d9bbf07863b97b3523b1b112844c43e713df8b66418a081fd1060b3b2",
    ),
    "eng.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/eng.traineddata",
        "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    ),
    "osd.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/osd.traineddata",
        "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
    ),
}

REQUIRED_FILES = ["tesseract.exe"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expected_sha: str | None) -> str:
    """fail-closed: 기대 checksum이 없거나 불일치하면 즉시 실패한다."""
    print(f"download: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())
    digest = sha256_of(dest)
    if not expected_sha:
        print(f"::error::checksum 미고정: {dest.name} — 스크립트에 sha256을 고정하세요.")
        sys.exit(1)
    if digest != expected_sha:
        print(f"::error::checksum 불일치: {dest.name} {digest} != {expected_sha}")
        sys.exit(1)
    print(f"  sha256={digest} size={dest.stat().st_size}")
    return digest


def main() -> None:
    if sys.platform != "win32":
        print("이 스크립트는 Windows CI 전용입니다 (설치 프로그램 추출).")
        sys.exit(1)

    DEST.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"tesseract_version": TESSERACT_VERSION, "files": {}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        installer = tmp_path / "tesseract-setup.exe"
        manifest["files"]["installer"] = {
            "url": TESSERACT_URL,
            "sha256": download(TESSERACT_URL, installer, TESSERACT_SHA256),
        }
        # NSIS 무설치 추출 (silent install to temp dir)
        install_dir = tmp_path / "tesseract"
        subprocess.run(
            ["cmd", "/c", "start", "/wait", "", str(installer), "/S", f"/D={install_dir}"],
            check=True,
            timeout=600,
        )
        # 설치기가 자식 프로세스로 분기해 먼저 반환하는 경우 대비: 완료를 폴링으로 확인
        deadline = time.monotonic() + 300
        default_dir = Path("C:/Program Files/Tesseract-OCR")  # /D 미적용 시 기본 경로
        while time.monotonic() < deadline:
            if (install_dir / "tesseract.exe").is_file():
                break
            if (default_dir / "tesseract.exe").is_file():
                install_dir = default_dir
                break
            time.sleep(2)
        else:
            print("::error::Tesseract 설치 추출 실패 — tesseract.exe를 찾지 못했습니다.")
            sys.exit(1)
        # 실행 파일 + DLL 복사
        for item in install_dir.iterdir():
            if item.suffix.lower() in (".exe", ".dll") and item.name.lower() != "uninstall.exe":
                shutil.copy2(item, DEST / item.name)
                manifest["files"][item.name] = {"sha256": sha256_of(DEST / item.name)}

    tessdata_dir = DEST / "tessdata"
    tessdata_dir.mkdir(exist_ok=True)
    for name, (url, sha) in TESSDATA.items():
        digest = download(url, tessdata_dir / name, sha)
        manifest["files"][f"tessdata/{name}"] = {"url": url, "sha256": digest}

    # TSV 출력 설정 — 없으면 tesseract가 종료 코드 0으로 빈 결과를 낸다
    configs_dir = tessdata_dir / "configs"
    configs_dir.mkdir(exist_ok=True)
    (configs_dir / "tsv").write_text("tessedit_create_tsv 1\n", encoding="utf-8")
    manifest["files"]["tessdata/configs/tsv"] = {"sha256": sha256_of(configs_dir / "tsv")}

    # 필수 파일 검증 — 누락 시 빌드 실패
    missing = [f for f in REQUIRED_FILES if not (DEST / f).is_file()]
    for lang in ("kor", "eng", "osd"):
        if not (tessdata_dir / f"{lang}.traineddata").is_file():
            missing.append(f"tessdata/{lang}.traineddata")
    if missing:
        print(f"::error::OCR 리소스 누락: {missing}")
        sys.exit(1)

    (DEST / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(f"OK: OCR 리소스 준비 완료 — 총 {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
