"""PyInstaller 엔트리포인트 — Tauri가 --host/--port 인자로 실행한다."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import os

    os.environ["MEDBRIDGE_BOUND_PORT"] = str(args.port)

    from app.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
