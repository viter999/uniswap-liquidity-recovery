"""Load rule (operator ruling 2026-10-17): run at below-normal priority; stop if free RAM < 3 GB.

apply() is called at the start of every pipeline entry point and by the test session (conftest.py).
It never lowers the threshold; MIN_FREE_GIB is a constant, not a parameter of the CLI.
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional

MIN_FREE_GIB = 3.0
BELOW_NORMAL_PRIORITY_CLASS = 0x4000


class LoadRuleStop(SystemExit):
    """Raised (as SystemExit) when the free-RAM rule forbids running."""


def free_ram_gib() -> Optional[float]:
    """Free physical RAM in GiB (Windows via GlobalMemoryStatusEx; None if unavailable)."""
    if os.name != "nt":
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page = os.sysconf("SC_PAGE_SIZE")
            return pages * page / 2 ** 30
        except (ValueError, AttributeError, OSError):
            return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    ms = MemoryStatusEx()
    ms.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return None
    return ms.ullAvailPhys / 2 ** 30


def set_below_normal_priority() -> bool:
    if os.name != "nt":
        try:
            os.nice(10)
            return True
        except (AttributeError, OSError):
            return False
    k = ctypes.windll.kernel32
    # Declare the handle types: without them GetCurrentProcess()'s 64-bit pseudo-handle is truncated to a 32-bit int
    # and SetPriorityClass fails silently (observed 2026-10-17: worker stayed at Normal priority).
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.SetPriorityClass.restype = ctypes.c_int
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k.GetPriorityClass.restype = ctypes.c_uint32
    ok = bool(k.SetPriorityClass(k.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))
    return ok and k.GetPriorityClass(k.GetCurrentProcess()) == BELOW_NORMAL_PRIORITY_CLASS


def apply(context: str = "pipeline") -> float:
    """Set priority and enforce the free-RAM rule. Returns free GiB. Raises LoadRuleStop if below MIN_FREE_GIB."""
    set_below_normal_priority()
    free = free_ram_gib()
    if free is None:
        print(f"[loadguard] {context}: free RAM unknown; continuing at below-normal priority", file=sys.stderr)
        return float("nan")
    if free < MIN_FREE_GIB:
        raise LoadRuleStop(f"LOAD RULE STOP ({context}): free RAM {free:.2f} GiB < {MIN_FREE_GIB:.1f} GiB")
    return free
