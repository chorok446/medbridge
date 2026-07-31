# PyInstaller spec — FastAPI sidecar 단일 실행 파일.
# GitHub Actions Windows runner에서 빌드한다 (로컬 macOS 빌드 산출물은 배포에 쓰지 않는다).
#
#   uv run pyinstaller sidecar.spec
#
# 산출물: dist/medbridge-sidecar(.exe)

import sys

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("aiosqlite")
    + collect_submodules("alembic")
    + ["app.main"]
)

a = Analysis(
    ["sidecar_entry.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("alembic", "alembic"),
        ("alembic.ini", "."),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="medbridge-sidecar",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)
