"""Threaded SDR capture engine for the GUI (v1.2).

Runs the v1.1 detection pipeline on a background thread and publishes updates
to a thread-safe queue the GUI drains. Decoupled from any UI toolkit and from
the SDR itself: pass a custom `source` (a callable returning IQ chunks) to run
headlessly in tests without hardware.

Queue messages (all dicts with a "type"):
    {"type": "spectrum", "freqs_mhz": ndarray, "row_db": ndarray}
    {"type": "ping",     "ping": Ping, "snapshot": str|None, "count": int}
    {"type": "snapshot", "path": str, "index": int}   # async render finished
    {"type": "snapshot_dropped", "count": int}        # worker fell behind
    {"type": "reconnect", "text": str}                # v1.3 auto-reconnect
    {"type": "status",   "text": str}
    {"type": "error",    "text": str}
    {"type": "stopped"}
"""
from __future__ import annotations

import datetime as dt
import queue
import threading
from typing import Callable, Optional

import numpy as np

from config import CONFIG, Config
from meteor_detector import (
    BandPowerAnalyzer, EventDetector, CsvSink, Ping, make_sdr,
)
from snapshot import (IQRingBuffer, write_snapshot, extract_window,
                      SnapshotWorker, SnapshotJob)
from classify import classify_with_config
from reconnect import ReconnectingReader
from uptime import UptimeLog

SampleSource = Callable[[int], np.ndarray]


class _SourceDev:
    """Adapts an injectable sample source to the SDR read/close interface so the
    reconnecting reader can drive it in headless tests."""

    def __init__(self, source: SampleSource) -> None:
        self._s = source
        self.center_freq = 0.0
        self.gain = 0.0

    def read_samples(self, n):
        return self._s(n)

    def close(self):
        pass


class CaptureEngine:
    def __init__(self, cfg: Config = CONFIG, source: Optional[SampleSource] = None) -> None:
        self.cfg = cfg
        self.q: "queue.Queue[dict]" = queue.Queue()
        self._source = source          # injectable; None => real SDR
        self._sdr = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Live-adjustable parameters (mutated by the GUI between reads).
        self.target_hz = cfg.reference_freq_hz
        self.gain = cfg.gain
        self.snr_threshold_db = cfg.snr_threshold_db
        self._retune = threading.Event()

        self.analyzer = BandPowerAnalyzer(cfg)
        self.detector = EventDetector(cfg, self.analyzer.block_dt_s)
        self.ping_count = 0

        # Waterfall decimation: publish one averaged spectrum row per read.
        freqs = np.fft.fftshift(np.fft.fftfreq(cfg.nfft, d=1.0 / cfg.sample_rate_hz))
        self._center_hz = cfg.reference_freq_hz - cfg.carrier_offset_hz
        self._freqs_mhz = (self._center_hz + freqs) / 1e6
        self._win = np.hanning(cfg.nfft)

    # ---- lifecycle -------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    # ---- live controls ---------------------------------------------------
    def set_threshold(self, db: float) -> None:
        self.snr_threshold_db = db
        self.detector.snr_threshold_db = db

    def request_retune(self, target_hz: float, gain) -> None:
        self.target_hz = target_hz
        self.gain = gain
        self._retune.set()

    # ---- worker ----------------------------------------------------------
    def _emit(self, **msg) -> None:
        self.q.put(msg)

    def _open_device(self):
        """Reader factory: (re)open a configured device using CURRENT target/gain.
        Also refreshes the frequency axis. Returns an SDR or the source adapter."""
        self._center_hz = self.target_hz - self.cfg.carrier_offset_hz
        freqs = np.fft.fftshift(
            np.fft.fftfreq(self.cfg.nfft, d=1.0 / self.cfg.sample_rate_hz))
        self._freqs_mhz = (self._center_hz + freqs) / 1e6
        if self._source is not None:
            return _SourceDev(self._source)
        sdr = make_sdr(self.cfg)
        sdr.center_freq = self._center_hz
        sdr.gain = self.gain
        return sdr

    def _run(self) -> None:
        cfg = self.cfg
        # Exposure log (v1.5): excludes warm-up and reconnect gaps. See uptime.py.
        uptime = UptimeLog(cfg.uptime_log, cfg.uptime_beat_s, cfg.uptime_enabled)
        reader = ReconnectingReader(
            cfg, open_fn=self._open_device,
            notify=lambda kind, msg: self._emit(
                type=("reconnect" if kind == "reconnect" else kind), text=msg),
            should_stop=self._stop.is_set,
            on_lost=lambda: uptime.off("reconnect"))
        try:
            reader.open()
        except Exception as e:  # noqa: BLE001
            hint = " (run tools/rtl-sdr/zadig.exe to bind WinUSB)" \
                if "LIBUSB_ERROR_NOT_FOUND" in str(e) else ""
            self._emit(type="error", text=f"{type(e).__name__}: {e}{hint}")
            self._emit(type="stopped")
            return
        self._emit(type="status",
                   text=f"Capturing {self.target_hz/1e6:.3f} MHz, gain {self.gain}")

        sink = CsvSink(cfg.log_csv)
        ring = worker = None
        if cfg.snapshot_enabled:
            ring = IQRingBuffer(cfg.ring_capacity_samples())
            if cfg.snapshot_async:
                worker = SnapshotWorker(
                    cfg,
                    on_saved=lambda p, i: self._emit(type="snapshot", path=p, index=i),
                    on_drop=lambda d: self._emit(type="snapshot_dropped", count=d))

        samples_seen = 0
        last_reconnects = 0
        try:
            while not self._stop.is_set():
                if self._retune.is_set():
                    self._retune.clear()
                    reader.sdr = self._open_device_retune(reader)

                iq = reader.read(cfg.read_block_size)
                if iq is None:
                    break  # stopped or gave up reconnecting
                now = dt.datetime.now(dt.timezone.utc)
                if reader.reconnects != last_reconnects:
                    last_reconnects = reader.reconnects
                    self.detector.abort_event()
                    if ring is not None:
                        ring.clear()   # sample-index continuity broke
                if ring is not None:
                    ring.append(iq)

                for i, power_db in enumerate(self.analyzer.iter_block_power_db(iq)):
                    ping = self.detector.update(
                        power_db, now, samples_seen + i * cfg.nfft)
                    if ping is not None:
                        self.ping_count += 1
                        snap = None
                        if ring is not None:
                            snap = self._handle_snapshot(
                                worker, ring, ping, self.ping_count - 1)
                        sink.write(ping)
                        self._emit(type="ping", ping=ping, snapshot=snap,
                                   count=self.ping_count)
                samples_seen += len(iq)

                # Exposure clock: starts when warm-up ends, restarts after a
                # reconnect gap (on_lost stopped it). Both calls are idempotent.
                if self.detector.warm:
                    uptime.on("detecting")
                    uptime.beat()

                nb = len(iq) // cfg.nfft
                if nb:
                    frames = iq[:nb * cfg.nfft].reshape(nb, cfg.nfft) * self._win
                    ps = np.fft.fftshift(
                        np.abs(np.fft.fft(frames, axis=1)) ** 2, axes=1)
                    row_db = 10 * np.log10(ps.mean(axis=0) + 1e-12)
                    self._emit(type="spectrum",
                               freqs_mhz=self._freqs_mhz, row_db=row_db)
        except Exception as e:  # noqa: BLE001
            self._emit(type="error", text=f"{type(e).__name__}: {e}")
        finally:
            reader.close()
            if worker is not None:
                worker.close()
            sink.close()
            uptime.close("stop")
            self._emit(type="stopped")

    def _open_device_retune(self, reader):
        """Apply a live retune to the open device (or reopen for the axis)."""
        dev = reader.sdr
        self._center_hz = self.target_hz - self.cfg.carrier_offset_hz
        freqs = np.fft.fftshift(
            np.fft.fftfreq(self.cfg.nfft, d=1.0 / self.cfg.sample_rate_hz))
        self._freqs_mhz = (self._center_hz + freqs) / 1e6
        if dev is not None and self._source is None:
            dev.center_freq = self._center_hz
            dev.gain = self.gain
        self._emit(type="status",
                   text=f"Retuned {self.target_hz/1e6:.3f} MHz, gain {self.gain}")
        return dev

    def _handle_snapshot(self, worker, ring, ping, index) -> Optional[str]:
        """Classify (sets ping.classification) and either enqueue an async render
        or render synchronously. Returns a path only for the synchronous path."""
        try:
            iq, first_abs = extract_window(
                ring, ping, self.cfg.sample_rate_hz,
                self.cfg.snapshot_pre_roll_s, self.cfg.snapshot_post_roll_s,
                max_window_s=self.cfg.snapshot_max_window_s)
            if len(iq) < self.cfg.snapshot_nfft:
                return None
            cls = classify_with_config(
                iq, ping, first_abs, self.cfg,
                center_hz=self._center_hz, target_hz=self.target_hz)
            ping.classification = cls.label
            if self.cfg.snapshot_skip_aircraft and cls.label == "aircraft":
                return None
            job = SnapshotJob(iq, first_abs, ping, self._center_hz,
                              self.target_hz, cls.label, index)
            if worker is not None:
                worker.submit(job)
                return None
            return write_snapshot(self.cfg, job)
        except Exception as e:  # noqa: BLE001
            self._emit(type="error", text=f"snapshot failed: {e}")
            return None
