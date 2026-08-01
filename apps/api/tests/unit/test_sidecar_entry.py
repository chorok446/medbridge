"""Windows sidecar process-tree bootstrap regression tests."""

import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are Windows-only")
def test_windows_sidecar_hook_joins_job_and_kills_descendants(tmp_path: Path) -> None:
    """The frozen-Python hook joins the named job before spawning descendants."""
    import ctypes
    from ctypes import wintypes

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x0000_2000
    process_terminate = 0x0001
    synchronize = 0x0010_0000
    wait_object_0 = 0

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job_name = f"Local\\MedBridgeSidecarTest-{uuid.uuid4()}"
    job = kernel32.CreateJobObjectW(None, job_name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    parent: subprocess.Popen[str] | None = None
    descendant = None
    try:
        limits = ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            job_object_limit_kill_on_job_close
        )
        configured = kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not configured:
            raise ctypes.WinError(ctypes.get_last_error())

        child_pid_file = tmp_path / "descendant.pid"
        script = textwrap.dedent(
            """
            import subprocess
            import sys
            import time
            from pathlib import Path

            from sidecar_entry import _join_windows_process_tree

            _join_windows_process_tree()
            child = subprocess.Popen(
                ["ping.exe", "-n", "30", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
            while True:
                time.sleep(1)
            """
        )
        env = os.environ.copy()
        env["MEDBRIDGE_WINDOWS_JOB_NAME"] = job_name
        api_root = Path(__file__).resolve().parents[2]
        parent = subprocess.Popen(
            [sys.executable, "-c", script, str(child_pid_file)],
            cwd=api_root,
            env=env,
            text=True,
        )

        deadline = time.monotonic() + 10
        while not child_pid_file.exists() and time.monotonic() < deadline:
            if parent.poll() is not None:
                pytest.fail(f"sidecar hook process exited early with {parent.returncode}")
            time.sleep(0.05)
        assert child_pid_file.exists(), "sidecar hook did not launch its descendant"
        descendant_pid = int(child_pid_file.read_text(encoding="ascii"))

        descendant = kernel32.OpenProcess(
            synchronize | process_terminate, False, descendant_pid
        )
        assert descendant, "descendant exited before Job Object close"
        assert kernel32.WaitForSingleObject(descendant, 0) != wait_object_0

        kernel32.CloseHandle(job)
        job = None
        parent.wait(timeout=5)

        assert (
            kernel32.WaitForSingleObject(descendant, 5000) == wait_object_0
        ), "closing the Job Object did not terminate the descendant"
    finally:
        if job:
            kernel32.CloseHandle(job)
        if parent is not None and parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if descendant:
            if kernel32.WaitForSingleObject(descendant, 0) != wait_object_0:
                kernel32.TerminateProcess(descendant, 1)
                kernel32.WaitForSingleObject(descendant, 5000)
            kernel32.CloseHandle(descendant)
