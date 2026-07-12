"""Dependency-free resident-set-size reporting (v1.4).

The Pi 3B+ has 1 GB shared with the OS, so a long unattended run should be able
to say how much memory it is actually using -- in the heartbeat, on the status
page, and in the soak test. psutil would do this, but it is a compiled
dependency we do not otherwise need on the Pi, so read it from the OS directly.
"""
from __future__ import annotations

import ctypes
import os
import sys


def rss_bytes() -> int:
    """Current resident set size in bytes, or 0 if it cannot be determined."""
    if sys.platform.startswith("linux"):
        try:
            # statm fields are in pages; field 1 is resident.
            with open("/proc/self/statm", "r") as f:
                resident = int(f.read().split()[1])
            return resident * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            return 0

    if sys.platform == "win32":
        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        kernel32 = ctypes.windll.kernel32
        # HANDLE is pointer-sized. Without an explicit restype ctypes treats it
        # as a 32-bit int, and the pseudo-handle (-1) arrives at the callee with
        # its upper bits garbage -- the call then just fails.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p

        # Modern Windows exports this from kernel32 as K32GetProcessMemoryInfo;
        # psapi.dll is the older home. Try both.
        for dll, name in ((kernel32, "K32GetProcessMemoryInfo"),
                          (ctypes.windll.psapi, "GetProcessMemoryInfo")):
            try:
                fn = getattr(dll, name)
            except (AttributeError, OSError):
                continue
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters),
                           ctypes.c_ulong]
            fn.restype = ctypes.c_int
            if fn(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                  counters.cb):
                return int(counters.WorkingSetSize)
        return 0

    return 0


def rss_mb() -> float:
    return rss_bytes() / (1024.0 * 1024.0)
