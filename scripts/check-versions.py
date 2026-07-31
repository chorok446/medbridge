#!/usr/bin/env python3
"""버전 일치 검증 — web package.json / tauri.conf.json / Python sidecar가 같아야 한다.

불일치 시 종료 코드 1 (CI 실패).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

web = json.loads((ROOT / "apps/web/package.json").read_text())["version"]
tauri = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())["version"]
desktop = json.loads((ROOT / "apps/desktop/package.json").read_text())["version"]

init_text = (ROOT / "apps/api/app/__init__.py").read_text()
match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
sidecar = match.group(1) if match else "(missing)"

pyproject = (ROOT / "apps/api/pyproject.toml").read_text()
match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
api_pkg = match.group(1) if match else "(missing)"

versions = {
    "apps/web/package.json": web,
    "apps/desktop/package.json": desktop,
    "apps/desktop/src-tauri/tauri.conf.json": tauri,
    "apps/api/app/__init__.py": sidecar,
    "apps/api/pyproject.toml": api_pkg,
}

print("버전 확인:")
for source, version in versions.items():
    print(f"  {source}: {version}")

if len(set(versions.values())) != 1:
    print("::error::버전이 일치하지 않습니다. 모든 파일의 버전을 동일하게 맞추세요.")
    sys.exit(1)

print(f"OK: 모든 구성요소가 {web} 로 일치")
