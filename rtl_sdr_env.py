"""Bootstrap the librtlsdr DLL location for pyrtlsdr on Windows.

Import this module BEFORE importing `rtlsdr` anywhere in the project:

    import rtl_sdr_env  # noqa: F401  (side effect: registers DLL dir)
    from rtlsdr import RtlSdr

This keeps us from having to put the DLLs on the system PATH.
"""
from __future__ import annotations

import os
import platform
import warnings
from pathlib import Path

# The bundled rtl-sdr-blog DLL lacks a few optional GPIO/dithering entry
# points that pyrtlsdr probes at import. We patched pyrtlsdr to warn instead
# of crash; none are needed here, so silence that one specific warning.
warnings.filterwarnings(
    "ignore",
    message=r"librtlsdr build is missing optional functions.*",
)

# Bundled rtl-sdr-blog Windows build lives under tools/rtl-sdr/<arch>/
_ROOT = Path(__file__).resolve().parent
_ARCH = "x64" if platform.machine().endswith("64") else "x86"
_DLL_DIR = _ROOT / "tools" / "rtl-sdr" / _ARCH


def register() -> Path:
    """Make the bundled librtlsdr build importable. Returns the DLL dir."""
    if os.name == "nt":
        if not _DLL_DIR.is_dir():
            raise FileNotFoundError(f"librtlsdr DLL folder not found: {_DLL_DIR}")
        # Preferred, isolated mechanism (Python 3.8+).
        os.add_dll_directory(str(_DLL_DIR))
        # Belt-and-suspenders: some loaders still consult PATH.
        os.environ["PATH"] = str(_DLL_DIR) + os.pathsep + os.environ.get("PATH", "")
    return _DLL_DIR


register()
