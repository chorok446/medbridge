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
    + collect_submodules("pymupdf")  # PDF 추출 엔진 (바이너리 포함)
    # keyring은 백엔드를 entry point로 동적 로드하므로 PyInstaller가 놓친다 —
    # 서브모듈 전체 + 플랫폼 백엔드를 명시적으로 포함해 API 키 저장이 동작하게 한다.
    + collect_submodules("keyring")
    + [
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.chainer",
        "keyring.backends.fail",
    ]
    + ["app.main"]
)

if sys.platform == "win32":
    # Windows Credential Manager 백엔드는 win32cred(pywin32)에 의존한다.
    hidden += collect_submodules("win32ctypes") + ["win32cred", "win32timezone"]

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
