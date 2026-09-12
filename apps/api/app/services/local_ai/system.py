"""시스템 자원 감지 — 총 RAM·디스크 여유. 안내용이며 실패 시 None(안내 생략)."""

import shutil


def total_ram_bytes() -> int | None:
    """총 물리 RAM(바이트). 감지 실패 시 None — 안내를 생략할 뿐 기능을 막지 않는다."""
    # Windows: GlobalMemoryStatusEx (ctypes)
    try:
        import ctypes

        if hasattr(ctypes, "windll"):

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
    except Exception:
        pass
    # POSIX: sysconf
    try:
        import os

        if not hasattr(os, "sysconf"):
            return None
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages) * int(page_size)
    except (ValueError, OSError, AttributeError):
        pass
    return None


def free_disk_bytes(path: str = "/") -> int | None:
    """지정 경로가 속한 볼륨의 여유 공간(바이트). 실패 시 None."""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None
