"""Headless memory soak test for the capture pipeline (v1.4).

Answers the question "will this survive a night on a 1 GB Pi 3B+?" without a Pi
and without hardware. Drives the real `CaptureEngine` with a synthetic IQ source
at the real sample rate, floods it with events (the SeaTac aircraft-burst case
that fills the snapshot queue), forces a reconnect, and reports peak RSS.

    python tools/mem_soak.py                  # default profile (Windows/GUI)
    python tools/mem_soak.py --profile pi     # the 1 GB Raspberry Pi budget
    python tools/mem_soak.py --profile pi --budget-mb 400

Feeds samples as fast as the pipeline will take them -- far faster than a real
dongle -- so the snapshot worker falls behind exactly as it would during a burst.
Writes its artifacts to a temp dir, never to the project's snapshots/.
"""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import sys
import tempfile
import time
from dataclasses import replace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memutil                                  # noqa: E402
from config import get_config                   # noqa: E402
from capture_engine import CaptureEngine        # noqa: E402


class SyntheticSource:
    """Noise + an in-band tone switched on and off to fake a storm of events.

    The schedule is deliberately hostile: back-to-back events with almost no
    quiet gap, including long (2 s) ones, which produce the biggest snapshot
    windows and so the biggest queued jobs.
    """

    def __init__(self, cfg, event_s: float, gap_s: float, long_every: int = 5,
                 fail_at_s: float | None = None) -> None:
        self.cfg = cfg
        self.event_s = event_s
        self.gap_s = gap_s
        self.long_every = long_every
        self.fail_at_s = fail_at_s
        self.failed = False
        self.pos = 0                 # absolute sample index
        self.rng = np.random.default_rng(1234)

    def _event_active(self, t: np.ndarray) -> np.ndarray:
        """Boolean per-sample: is an event on at stream time t?"""
        warm = self.cfg.warmup_skip_s + 2.0     # let the baseline settle first
        cycle = self.event_s + self.gap_s
        n = np.floor(np.maximum(0.0, t - warm) / cycle)
        phase = np.maximum(0.0, t - warm) % cycle
        # Every Nth event is a long one -- the worst case for window size.
        dur = np.where(n % self.long_every == (self.long_every - 1),
                       min(2.0, self.cfg.max_ping_ms / 1000.0), self.event_s)
        return (t >= warm) & (phase < dur)

    def __call__(self, n: int) -> np.ndarray:
        rate = self.cfg.sample_rate_hz
        t0 = self.pos / rate
        if (self.fail_at_s is not None and not self.failed and t0 >= self.fail_at_s):
            self.failed = True
            raise OSError("simulated USB read error (rtlsdr: LIBUSB_ERROR_IO)")

        idx = self.pos + np.arange(n)
        t = idx / rate
        iq = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        iq = iq.astype(np.complex64) * np.float32(0.02)

        on = self._event_active(t)
        if on.any():
            # Strong tone at the carrier offset -> lands inside the detect band.
            tone = np.exp(2j * np.pi * self.cfg.carrier_offset_hz * t[on])
            iq[on] += (tone * 0.9).astype(np.complex64)

        self.pos += n
        return iq


def soak(profile: str | None, seconds: float, budget_mb: float,
         event_ms: float, gap_ms: float) -> int:
    cfg = get_config(profile)
    tmp = tempfile.mkdtemp(prefix="meteor_soak_")
    cfg = replace(cfg,
                  snapshot_dir=os.path.join(tmp, "snapshots"),
                  log_csv=os.path.join(tmp, "events.csv"))

    print(f"profile           : {profile or 'default'}")
    print(f"snapshot_mode     : {cfg.snapshot_mode}")
    print(f"ring              : {cfg.ring_span_seconds():.1f} s "
          f"({cfg.ring_capacity_samples() * 8 / 1e6:.0f} MB)")
    print(f"queue cap         : {cfg.snapshot_queue_max_bytes / 1e6:.0f} MB "
          f"/ {cfg.snapshot_queue_max} jobs")
    print(f"window cap        : "
          f"{cfg.snapshot_max_window_s or 0:.1f} s"
          f"{' (uncapped)' if not cfg.snapshot_max_window_s else ''}")
    print(f"skip aircraft     : {cfg.snapshot_skip_aircraft}")
    print(f"baseline RSS      : {memutil.rss_mb():.1f} MB")
    print(f"\nflooding with ~{event_ms:.0f} ms events every "
          f"{event_ms + gap_ms:.0f} ms for {seconds:.0f} s "
          f"(reconnect forced mid-run)...\n")

    source = SyntheticSource(cfg, event_ms / 1000.0, gap_ms / 1000.0,
                             fail_at_s=cfg.warmup_skip_s + 12.0)
    engine = CaptureEngine(cfg, source=source)

    peak_rss = memutil.rss_mb()
    pings = drops = reconnects = 0
    t_end = time.time() + seconds
    engine.start()
    try:
        while time.time() < t_end and engine.running:
            try:
                msg = engine.q.get(timeout=0.2)
            except queue.Empty:
                msg = None
            if msg:
                kind = msg.get("type")
                if kind == "ping":
                    pings += 1
                elif kind == "snapshot_dropped":
                    drops = msg["count"]
                elif kind == "reconnect":
                    reconnects += 1
                elif kind == "error":
                    print(f"  [error] {msg['text']}")
            peak_rss = max(peak_rss, memutil.rss_mb())
    finally:
        engine.stop()
        peak_rss = max(peak_rss, memutil.rss_mb())

    written = len(os.listdir(cfg.snapshot_dir)) \
        if os.path.isdir(cfg.snapshot_dir) else 0
    bytes_on_disk = sum(
        os.path.getsize(os.path.join(cfg.snapshot_dir, f))
        for f in os.listdir(cfg.snapshot_dir)) if written else 0
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\npings detected    : {pings}")
    print(f"snapshots written : {written} "
          f"({bytes_on_disk / 1e6:.1f} MB, "
          f"{bytes_on_disk / max(1, written) / 1e6:.2f} MB each)")
    print(f"snapshots dropped : {drops}  (events still logged + classified)")
    print(f"reconnect events  : {reconnects}  "
          f"(pipeline {'RECOVERED' if pings and engine.ping_count else 'STALLED'})")
    print(f"PEAK RSS          : {peak_rss:.1f} MB   (budget {budget_mb:.0f} MB)")

    if peak_rss > budget_mb:
        print(f"\nFAIL: peak RSS {peak_rss:.1f} MB exceeds the "
              f"{budget_mb:.0f} MB budget.")
        return 1
    print(f"\nPASS: stayed within the {budget_mb:.0f} MB budget.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", default=None, help="config profile (e.g. 'pi')")
    p.add_argument("--seconds", type=float, default=45.0,
                   help="wall-clock seconds to run (default: %(default)s)")
    p.add_argument("--budget-mb", type=float, default=400.0,
                   help="fail if peak RSS exceeds this (default: %(default)s)")
    p.add_argument("--event-ms", type=float, default=300.0)
    p.add_argument("--gap-ms", type=float, default=150.0)
    args = p.parse_args(argv)
    return soak(args.profile, args.seconds, args.budget_mb,
                args.event_ms, args.gap_ms)


if __name__ == "__main__":
    sys.exit(main())
