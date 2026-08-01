"""PyInstaller 엔트리포인트 — Tauri가 --host/--port 인자로 실행한다."""

import argparse
import os


def _join_windows_process_tree() -> None:
    """Join the Tauri-owned Job Object before importing the application.

    PyInstaller one-file mode starts a bootloader parent and a separate Python
    child. The Rust shell assigns the parent, while this early hook repairs the
    case where the child was created just before that assignment. It does not
    make the Rust spawn-to-assignment interval atomic.
    """
    job_name = os.environ.pop("MEDBRIDGE_WINDOWS_JOB_NAME", None)
    if os.name != "nt" or not job_name:
        return

    import ctypes
    from ctypes import wintypes

    job_object_assign_process = 0x0001
    job_object_query = 0x0004
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.OpenJobObjectW(
        job_object_assign_process | job_object_query,
        False,
        job_name,
    )
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        process = kernel32.GetCurrentProcess()
        already_joined = wintypes.BOOL()
        if not kernel32.IsProcessInJob(process, job, ctypes.byref(already_joined)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not already_joined.value and not kernel32.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(job)


def main() -> None:
    _join_windows_process_tree()

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    os.environ["MEDBRIDGE_BOUND_PORT"] = str(args.port)

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
