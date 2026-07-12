"""Make an installed pyrtlsdr tolerant of a librtlsdr build that is missing a few
optional functions (rtlsdr_set_dithering and the GPIO helpers).

Some librtlsdr builds (incl. the rtl-sdr-blog fork and some distro packages) do
not export these, yet pyrtlsdr binds them unconditionally at import AND calls
rtlsdr_set_dithering inside RtlSdr.__init__ -> `import rtlsdr` / open raises
AttributeError. This injects, right after the library is loaded in
rtlsdr/librtlsdr.py, a block that installs harmless no-op stubs (return 0) for
any missing optional function. Idempotent; keeps a .bak.

    python deploy/pi/patch_pyrtlsdr.py
"""
from __future__ import annotations

import importlib.util
import os
import re

MARKER = "# --- radio_meteor_tracker optional-binding patch ---"
OPTIONAL = [
    "rtlsdr_set_dithering", "rtlsdr_set_gpio_output", "rtlsdr_set_gpio_input",
    "rtlsdr_set_gpio_bit", "rtlsdr_get_gpio_bit", "rtlsdr_set_gpio_byte",
    "rtlsdr_get_gpio_byte", "rtlsdr_set_gpio_status",
]

BLOCK = '''
{marker}
def _rmt_opt_stub(_name):
    def _s(*_a, **_k):
        return 0
    _s.__name__ = _name
    return _s
for _rmt_n in {names!r}:
    try:
        getattr(librtlsdr, _rmt_n)
    except AttributeError:
        setattr(librtlsdr, _rmt_n, _rmt_opt_stub(_rmt_n))
# --- end patch ---
'''


def main() -> int:
    spec = importlib.util.find_spec("rtlsdr.librtlsdr")
    path = spec.origin if spec else None
    if not path or not os.path.exists(path):
        print("Could not locate rtlsdr/librtlsdr.py -- is pyrtlsdr installed?")
        return 1

    src = open(path, encoding="utf-8").read()
    if MARKER in src:
        print(f"Already patched: {path}")
        return 0

    # Insert right after the library is assigned: 'librtlsdr = ...'
    m = re.search(r"^librtlsdr\s*=\s*.+$", src, re.MULTILINE)
    if not m:
        print("Could not find the 'librtlsdr = ...' load line; not patching.")
        return 2

    block = BLOCK.format(marker=MARKER, names=OPTIONAL)
    patched = src[:m.end()] + "\n" + block + src[m.end():]
    open(path + ".bak", "w", encoding="utf-8").write(src)
    open(path, "w", encoding="utf-8").write(patched)
    print(f"Patched {path}\n  backup: {path}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
