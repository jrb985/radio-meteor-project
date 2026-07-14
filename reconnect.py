"""Auto-reconnecting SDR reader (v1.3).

Wraps sdr.read_samples so a USB/read error does not end a run. On failure it
closes, backs off (exponential, capped), reopens via a caller-supplied factory,
and resumes -- essential for long unattended runs and the headless Pi service.

Toolkit-agnostic: the caller passes an `open_fn` that returns a fully configured,
opened SDR, a `notify(kind, msg)` callback, and a `should_stop()` predicate so a
threaded caller can break out during backoff.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

import numpy as np


class ReconnectingReader:
    def __init__(self, cfg, open_fn: Callable[[], object],
                 notify: Optional[Callable[[str, str], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None,
                 on_lost: Optional[Callable[[], None]] = None,
                 on_restored: Optional[Callable[[], None]] = None) -> None:
        self.cfg = cfg
        self.open_fn = open_fn
        self.notify = notify or (lambda kind, msg: None)
        self.should_stop = should_stop or (lambda: False)
        # Fired at the MOMENT the device is lost and the moment it is back, so a
        # caller (uptime.py) can exclude the gap from observing exposure. The
        # read() call blocks across the whole backoff, so the caller cannot time
        # the gap itself -- only the reader knows when it actually began.
        self.on_lost = on_lost or (lambda: None)
        self.on_restored = on_restored or (lambda: None)
        self.sdr = None
        self.reconnects = 0          # increments on each successful reopen

    def open(self):
        self.sdr = self.open_fn()
        return self.sdr

    def read(self, n: int) -> Optional[np.ndarray]:
        """Return IQ, reconnecting on error. Returns None only if we stopped or
        gave up (caller should then break)."""
        while True:
            try:
                return self.sdr.read_samples(n)
            except Exception as e:  # noqa: BLE001
                if not self.cfg.reconnect_enabled:
                    raise
                if not self._reconnect(e):
                    return None      # stopped or gave up

    def _reconnect(self, err) -> bool:
        self.on_lost()               # exposure stops HERE, not when read() returns
        try:
            if self.sdr is not None:
                self.sdr.close()
        except Exception:  # noqa: BLE001
            pass
        self.sdr = None

        backoff = self.cfg.reconnect_backoff_start_s
        attempt = 0
        while not self.should_stop():
            attempt += 1
            self.notify("reconnect",
                        f"read error ({err}); reopening (attempt {attempt}, "
                        f"wait {backoff:.0f}s)")
            # Sleep in small slices so should_stop() stays responsive.
            slept = 0.0
            while slept < backoff and not self.should_stop():
                time.sleep(min(0.25, backoff - slept))
                slept += 0.25
            if self.should_stop():
                break
            try:
                self.sdr = self.open_fn()
                self.reconnects += 1
                self.notify("reconnect", f"reconnected (total {self.reconnects})")
                self.on_restored()
                return True
            except Exception as e2:  # noqa: BLE001
                err = e2
                backoff = min(backoff * 2.0, self.cfg.reconnect_backoff_max_s)
                give = self.cfg.reconnect_give_up_after
                if give and attempt >= give:
                    self.notify("error",
                                f"giving up after {attempt} reconnect attempts: {e2}")
                    return False
        return False  # stopped

    def close(self) -> None:
        if self.sdr is not None:
            try:
                self.sdr.close()
            except Exception:  # noqa: BLE001
                pass
        self.sdr = None
