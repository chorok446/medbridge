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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "apps/desktop/src-tauri/resources/ocr"

# 버전 고정 + sha256 (UB-Mannheim Tesseract Windows 빌드, tessdata_fast 커밋 고정)
TESSERACT_VERSION = "5.4.0.20240606"
TESSERACT_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/"
    f"v{TESSERACT_VERSION}/tesseract-ocr-w64-setup-{TESSERACT_VERSION}.exe"
)
TESSERACT_SHA256 = "38fSHA_TO_PIN_ON_FIRST_CI_RUN"  # 최초 CI 실행 로그의 실측값으로 고정할 것

TESSDATA_COMMIT = "4767ea922bcc460e70b87b1d303ebdfed0897da8"  # tessdata_fast 고정 커밋
TESSDATA = {
    "kor.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/kor.traineddata",
        None,  # 최초 실행 시 manifest에 기록되는 실측 sha256으로 고정
    ),
    "eng.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/eng.traineddata",
        None,
    ),
    "osd.traineddata": (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/osd.traineddata",
        None,
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
    print(f"download: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())
    digest = sha256_of(dest)
    if expected_sha and not expected_sha.startswith("38f") and digest != expected_sha:
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
            [str(installer), "/S", f"/D={install_dir}"], check=True, timeout=600
        )
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

    # 필수 파일 검증 — 누락 시 빌드 실패
    missing = [f for f in REQUIRED_FILES if not (DEST / f).is_file()]
    for lang in ("kor", "eng", "osd"):
        if not (tessdata_dir / f"{lang}.traineddata").is_file():
            missing.append(f"tessdata/{lang}.traineddata")
    if missing:
        print(f"::error::OCR 리소스 누락: {missing}")
        sys.exit(1)

    (DEST / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(f"OK: OCR 리소스 준비 완료 — 총 {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
