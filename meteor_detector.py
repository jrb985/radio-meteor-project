"""Meteor forward-scatter detector (v1.1: log + count + trigger snapshots).

Pipeline:
    RTL-SDR IQ stream  ->  FFT band-power around the reference carrier
                       ->  noise-floor baseline (EMA)
                       ->  threshold + hysteresis event detection
                       ->  CSV log (one row per ping) + running count
                       ->  high-res spectrogram snapshot per ping (v1.1)

Run AFTER the WinUSB driver is bound (see check_device.py / Zadig):
    python meteor_detector.py

Stop with Ctrl+C (or SIGTERM: systemctl stop / `timeout` / OS shutdown) -- both
trigger a clean shutdown that finalizes the CSV and the exposure log. v1.1 adds a
rolling IQ buffer so each ping is saved as a zoomed spectrogram under snapshots/
for meteor/aircraft/interference triage.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import signal
import sys
import time
from dataclasses import dataclass

import rtl_sdr_env  # noqa: F401  (registers the bundled librtlsdr DLL dir)
import numpy as np
from rtlsdr import RtlSdr

import memutil
from config import CONFIG, Config, get_config
from snapshot import (IQRingBuffer, write_snapshot, extract_window,
                      SnapshotWorker, SnapshotJob)
from classify import classify_with_config
from reconnect import ReconnectingReader
from uptime import UptimeLog


@dataclass
class Ping:
    start: dt.datetime
    duration_s: float
    peak_snr_db: float
    peak_power_db: float
    start_sample: int = 0   # absolute IQ index where the event began
    end_sample: int = 0     # absolute IQ index where the event ended
    classification: str = ""  # meteor / aircraft / interference (v1.2)


class BandPowerAnalyzer:
    """Turns IQ blocks into band power (dB) around the carrier offset."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.nfft = cfg.nfft
        self.window = np.hanning(cfg.nfft)
        freqs = np.fft.fftfreq(cfg.nfft, d=1.0 / cfg.sample_rate_hz)
        lo = cfg.carrier_offset_hz - cfg.detect_halfwidth_hz
        hi = cfg.carrier_offset_hz + cfg.detect_halfwidth_hz
        self.band_mask = (freqs >= lo) & (freqs <= hi)
        if not self.band_mask.any():
            raise ValueError(
                "Detection band falls outside the sampled bandwidth; "
                "check carrier_offset_hz / detect_halfwidth_hz / sample_rate_hz."
            )
        self.block_dt_s = cfg.nfft / cfg.sample_rate_hz

    def iter_block_power_db(self, iq: np.ndarray):
        """Yield band power (dB) for each full nfft block in an IQ chunk."""
        n_blocks = len(iq) // self.nfft
        for i in range(n_blocks):
            block = iq[i * self.nfft : (i + 1) * self.nfft] * self.window
            spec = np.fft.fft(block)
            power = np.abs(spec[self.band_mask]) ** 2
            yield 10.0 * np.log10(power.sum() + 1e-12)


class EventDetector:
    """Threshold + hysteresis over a stream of per-block band-power values."""

    def __init__(self, cfg: Config, block_dt_s: float) -> None:
        self.cfg = cfg
        self.block_dt_s = block_dt_s
        # Live-adjustable (GUI, v1.2); defaults come from config.
        self.snr_threshold_db = cfg.snr_threshold_db
        # EMA smoothing factor for the noise floor.
        self.alpha = 1.0 - np.exp(-block_dt_s / cfg.baseline_tau_s)
        self.baseline_db: float | None = None
        self.in_event = False
        self._ev_start: dt.datetime | None = None
        self._ev_start_sample = 0
        self._ev_blocks = 0
        self._ev_peak_snr = -np.inf
        self._ev_peak_pow = -np.inf
        self._nfft = cfg.nfft
        # Settle the tuner/AGC and let the baseline converge before detecting.
        self._warmup_blocks = int(cfg.warmup_skip_s / block_dt_s)
        self._n = 0

    def update(self, power_db: float, now: dt.datetime,
               sample_pos: int = 0) -> Ping | None:
        """Process one block. `sample_pos` is the absolute IQ index of the
        block's first sample (used to locate the event for snapshots)."""
        if self.baseline_db is None:
            self.baseline_db = power_db
            return None

        # During warm-up, only let the baseline converge -- no detections.
        self._n += 1
        if self._n <= self._warmup_blocks:
            self.baseline_db += self.alpha * (power_db - self.baseline_db)
            return None

        snr = power_db - self.baseline_db
        thr = self.snr_threshold_db
        end_thr = thr - self.cfg.hysteresis_db

        if not self.in_event:
            # Track the noise floor only while quiet.
            self.baseline_db += self.alpha * (power_db - self.baseline_db)
            if snr >= thr:
                self.in_event = True
                self._ev_start = now
                self._ev_start_sample = sample_pos
                self._ev_blocks = 1
                self._ev_peak_snr = snr
                self._ev_peak_pow = power_db
            return None

        # Inside an event.
        self._ev_blocks += 1
        self._ev_peak_snr = max(self._ev_peak_snr, snr)
        self._ev_peak_pow = max(self._ev_peak_pow, power_db)
        if snr < end_thr:
            duration_s = self._ev_blocks * self.block_dt_s
            self.in_event = False
            dur_ms = duration_s * 1000.0
            if self.cfg.min_ping_ms <= dur_ms <= self.cfg.max_ping_ms:
                return Ping(
                    start=self._ev_start,               # type: ignore[arg-type]
                    duration_s=duration_s,
                    peak_snr_db=self._ev_peak_snr,
                    peak_power_db=self._ev_peak_pow,
                    start_sample=self._ev_start_sample,
                    end_sample=sample_pos + self._nfft,
                )
        return None

    @property
    def warm(self) -> bool:
        """True once warm-up is over and pings can actually be detected.

        Warm-up is NOT observing time -- the detector is deliberately blind
        during it -- so exposure must not start counting until this flips.
        """
        return self.baseline_db is not None and self._n > self._warmup_blocks

    def abort_event(self) -> None:
        """Discard any in-progress event (e.g. after a reconnect gap)."""
        self.in_event = False


class CsvSink:
    HEADER = ["start_utc", "duration_s", "peak_snr_db", "peak_power_db",
              "classification"]

    def __init__(self, path: str) -> None:
        self.path = path
        self.count = 0
        # If an existing file uses an older/different header, rotate it aside so
        # the log stays schema-consistent (v1.2 added the classification column).
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
            if first != ",".join(self.HEADER):
                os.replace(path, path + ".old")
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        if new_file:
            self._w.writerow(self.HEADER)
            self._f.flush()

    def write(self, p: Ping) -> None:
        self.count += 1
        self._w.writerow(
            [
                p.start.isoformat(),
                f"{p.duration_s:.3f}",
                f"{p.peak_snr_db:.1f}",
                f"{p.peak_power_db:.1f}",
                p.classification,
            ]
        )
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def make_sdr(cfg: Config) -> RtlSdr:
    sdr = RtlSdr()
    sdr.sample_rate = cfg.sample_rate_hz
    sdr.center_freq = cfg.reference_freq_hz - cfg.carrier_offset_hz
    sdr.freq_correction = cfg.freq_correction_ppm or 1  # 0 raises on some tuners
    sdr.gain = cfg.gain
    return sdr


def _classify_event(cfg: Config, ring: IQRingBuffer, ping: Ping):
    """Extract event IQ and classify it (sets ping.classification). Returns
    (iq, first_abs, label) or None. Rendering is deferred (v1.3 async)."""
    center_hz = cfg.reference_freq_hz - cfg.carrier_offset_hz
    iq, first_abs = extract_window(
        ring, ping, cfg.sample_rate_hz,
        cfg.snapshot_pre_roll_s, cfg.snapshot_post_roll_s,
        max_window_s=cfg.snapshot_max_window_s)
    if len(iq) < cfg.snapshot_nfft:
        return None
    cls = classify_with_config(
        iq, ping, first_abs, cfg,
        center_hz=center_hz, target_hz=cfg.reference_freq_hz)
    ping.classification = cls.label
    return iq, first_abs, cls.label


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--profile", default=None,
        help="config profile to apply (e.g. 'pi' for the 1 GB Raspberry Pi "
             "budget). Defaults to $METEOR_PROFILE, else the plain defaults.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = get_config(args.profile) if args.profile else CONFIG
    except ValueError as e:
        print(e)
        return 2
    analyzer = BandPowerAnalyzer(cfg)
    detector = EventDetector(cfg, analyzer.block_dt_s)
    sink = CsvSink(cfg.log_csv)
    center_hz = cfg.reference_freq_hz - cfg.carrier_offset_hz

    # Rolling IQ buffer + (optional) async snapshot worker.
    ring = None
    worker = None
    if cfg.snapshot_enabled:
        ring = IQRingBuffer(cfg.ring_capacity_samples())
        if cfg.snapshot_async:
            worker = SnapshotWorker(
                cfg,
                on_saved=lambda p, i: print(f"    snapshot -> {p}"),
                on_drop=lambda d: print(f"    [snapshot dropped ({d} total): worker busy]"))

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    # Handle SIGINT (Ctrl+C) AND SIGTERM (systemctl stop, `timeout`, RuntimeMaxSec,
    # OS shutdown) identically: flip `running` so the main loop falls into the
    # finally block, which finalizes the CSV, flushes queued snapshots, and closes
    # the exposure log. Without the SIGTERM line, a service stop or a field-run
    # timeout kills the process mid-write -- losing the exposure finalize.
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Observing-exposure log. Exposure excludes warm-up and reconnect gaps, so
    # it is driven by the detector's `warm` flag and the reader's lost/restored
    # callbacks -- never by wall-clock or by when events happened to be logged.
    uptime = UptimeLog(cfg.uptime_log, cfg.uptime_beat_s, cfg.uptime_enabled)

    reader = ReconnectingReader(
        cfg, open_fn=lambda: make_sdr(cfg),
        notify=lambda kind, msg: print(f"  [{kind}] {msg}"),
        should_stop=lambda: not running,
        on_lost=lambda: uptime.off("reconnect"),
        on_restored=lambda: None)   # exposure resumes below, once detecting again
    try:
        reader.open()
    except Exception as e:  # noqa: BLE001
        print(f"Could not open SDR: {type(e).__name__}: {e}")
        if "LIBUSB_ERROR_NOT_FOUND" in str(e):
            print("-> No WinUSB driver bound. Run tools/rtl-sdr/zadig.exe.")
        return 2

    how = "async" if worker else ("sync" if ring else "off")
    ring_mb = (ring.capacity * 8 / 1e6) if ring else 0.0
    print(
        f"Listening: carrier {cfg.reference_freq_hz/1e6:.4f} MHz "
        f"(tuned {reader.sdr.center_freq/1e6:.4f} MHz), "
        f"band +/-{cfg.detect_halfwidth_hz/1e3:.1f} kHz, "
        f"block {analyzer.block_dt_s*1e3:.1f} ms. "
        f"Snapshots {cfg.snapshot_mode}/{how}, "
        f"reconnect {'on' if cfg.reconnect_enabled else 'off'}. "
        f"Ring {cfg.ring_span_seconds():.1f} s ({ring_mb:.0f} MB), "
        f"snapshot queue cap {cfg.snapshot_queue_max_bytes/1e6:.0f} MB, "
        f"RSS {memutil.rss_mb():.0f} MB. Ctrl+C to stop."
    )

    samples_seen = 0          # absolute IQ index of the current read's start
    last_reconnects = 0
    start_t = last_hb = time.time()
    try:
        while running:
            iq = reader.read(cfg.read_block_size)
            if iq is None:
                break  # stopped, or gave up reconnecting
            now = dt.datetime.now(dt.timezone.utc)
            if reader.reconnects != last_reconnects:
                last_reconnects = reader.reconnects
                detector.abort_event()  # discard any event spanning the gap
                if ring is not None:
                    ring.clear()        # sample-index continuity broke
            if ring is not None:
                ring.append(iq)
            for i, power_db in enumerate(analyzer.iter_block_power_db(iq)):
                sample_pos = samples_seen + i * cfg.nfft
                ping = detector.update(power_db, now, sample_pos)
                if ping is not None:
                    if ring is not None:
                        res = _classify_event(cfg, ring, ping)
                        if res is not None:
                            iqw, fa, label = res
                            skip = ((cfg.snapshot_skip_aircraft and label == "aircraft")
                                    or (cfg.snapshot_skip_interference
                                        and label == "interference"))
                            job = SnapshotJob(iqw, fa, ping, center_hz,
                                              cfg.reference_freq_hz, label,
                                              sink.count)
                            if not skip and worker is not None:
                                worker.submit(job)
                            elif not skip:  # synchronous fallback
                                try:
                                    write_snapshot(cfg, job)
                                except Exception as e:  # noqa: BLE001
                                    print(f"    snapshot failed: {e}")
                    sink.write(ping)
                    print(
                        f"PING #{sink.count}  {ping.start.strftime('%H:%M:%S')}  "
                        f"{ping.duration_s*1e3:6.0f} ms  "
                        f"SNR {ping.peak_snr_db:4.1f} dB  [{ping.classification or '?'}]"
                    )
            samples_seen += len(iq)

            # Exposure clock. `on()` is idempotent, so this both starts the
            # clock the moment warm-up ends and restarts it after a reconnect
            # gap (the reader's on_lost stopped it). `beat()` self-throttles.
            if detector.warm:
                uptime.on("detecting")
                uptime.beat()

            if time.time() - last_hb >= 300:  # periodic heartbeat
                last_hb = time.time()
                up = int(last_hb - start_t)
                extra = (f", snaps {worker.saved} (dropped {worker.dropped}, "
                         f"queued {worker.queued_bytes/1e6:.0f} MB)"
                         if worker is not None else "")
                print(f"  [heartbeat] up {up//3600}h{(up % 3600)//60}m, "
                      f"pings {sink.count}, reconnects {reader.reconnects}"
                      f", RSS {memutil.rss_mb():.0f} MB{extra}")
    finally:
        reader.close()
        if worker is not None:
            worker.close()
        sink.close()
        uptime.close("stop")
        print(f"\nStopped. {sink.count} pings logged to {cfg.log_csv}")
        if cfg.uptime_enabled:
            print(f"Observing exposure logged to {cfg.uptime_log} "
                  f"(run: python diurnal.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
