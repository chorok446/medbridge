"""플랫폼별 RAM 감지 API 유무를 안전하게 처리한다."""

import ctypes
import os

from app.services.local_ai.system import total_ram_bytes


def test_missing_platform_apis_returns_unknown(monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    monkeypatch.delattr(os, "sysconf", raising=False)
    assert total_ram_bytes() is None


def test_posix_fallback_uses_page_count_and_size(monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    values = {"SC_PHYS_PAGES": 4096, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(os, "sysconf", values.__getitem__, raising=False)
    assert total_ram_bytes() == 4096 * 4096
