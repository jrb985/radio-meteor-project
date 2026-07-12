"""Trigger-based event snapshots (v1.1; low-memory rework in v1.4).

When the detector flags a ping, we want to *see* that exact moment at high
resolution so it can be classified:
  * meteor       -> brief localized streak at the target channel, maybe a
                    short Doppler tail
  * aircraft     -> a carrier line that SWEEPS in frequency over seconds
  * interference -> broadband smear across the whole window

`IQRingBuffer` keeps the most recent few seconds of raw IQ so that, when a
ping completes, the window covering pre-roll + event + post-roll can be
captured. That window is then either rendered to a PNG here, or saved as a
compact .npz for later rendering -- see `snapshot_mode` below.

v1.4 (1 GB Raspberry Pi budget). Three things changed, all about memory:

  * The ring is now a PREALLOCATED circular buffer. It used to np.concatenate a
    fresh array on every read, which at 1.024 Msps meant two ~33 MB allocations
    four times a second -- ~260 MB/s of memcpy and a fragmented heap after a
    12 h run.
  * `SnapshotWorker`'s queue is bounded by BYTES, not by job count. Each job
    carries a whole IQ window (up to ~25 MB for a long event), so a 32-job
    bound was really a ~790 MB bound -- enough to OOM a Pi during an aircraft
    burst, which is precisely when the queue fills.
  * matplotlib is imported LAZILY, inside the PNG renderer only. In the Pi's
    `snapshot_mode="npz"` the capture process never imports it at all, which
    keeps ~90 MB of RSS (and the multi-second render) off the box entirely.

snapshot_mode:
    "png" -- render a spectrogram PNG here (default; what the Windows GUI needs)
    "npz" -- save a decimated float16 spectrogram + metadata (~1-2 MB) and let
             render_snapshots.py turn it into a PNG later on a real machine
    "off" -- classify and log only, no snapshot artifact
"""
from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass

import numpy as np

import dsp


class IQRingBuffer:
    """Fixed-capacity rolling buffer of the most recent IQ samples.

    Tracks absolute sample indices so a detector working in absolute stream
    coordinates can ask for an arbitrary [start, stop) window.

    Preallocated and circular: `append` copies into the ring in at most two
    memcpys and allocates nothing (v1.4).
    """

    def __init__(self, capacity_samples: int) -> None:
        self.capacity = max(1, int(capacity_samples))
        self._buf = np.zeros(self.capacity, dtype=np.complex64)
        self._pos = 0     # next write offset within the ring
        self._total = 0   # total samples ever appended
        self._base = 0    # absolute index of the OLDEST retained sample

    def append(self, chunk: np.ndarray) -> None:
        n = len(chunk)
        if n == 0:
            return
        if n >= self.capacity:
            # This chunk alone overruns the ring: keep only its tail.
            self._buf[:] = chunk[-self.capacity:].astype(np.complex64, copy=False)
            self._pos = 0
            self._total += n
            self._base = self._total - self.capacity
            return

        end = self._pos + n
        if end <= self.capacity:
            self._buf[self._pos:end] = chunk
        else:  # wrap
            split = self.capacity - self._pos
            self._buf[self._pos:] = chunk[:split]
            self._buf[: n - split] = chunk[split:]
        self._pos = end % self.capacity
        self._total += n
        self._base = max(0, self._total - self.capacity)

    def clear(self) -> None:
        """Drop retained samples (sample-index continuity broke, e.g. after a
        reconnect) without reallocating."""
        self._pos = 0
        self._base = self._total

    @property
    def total_appended(self) -> int:
        return self._total

    @property
    def retained(self) -> int:
        return self._total - self._base

    def get(self, start_abs: int, stop_abs: int) -> tuple[np.ndarray, int]:
        """Return IQ for [start_abs, stop_abs), clamped to what is retained.

        Also returns the absolute index of the first returned sample so the
        caller can build a correct time axis.
        """
        i0 = max(int(start_abs), self._base)
        i1 = min(int(stop_abs), self._total)
        if i1 <= i0:
            return np.empty(0, dtype=np.complex64), i0

        n = i1 - i0
        # Where the oldest retained sample sits inside the ring.
        oldest = (self._pos - self.retained) % self.capacity
        start = (oldest + (i0 - self._base)) % self.capacity
        end = start + n
        if end <= self.capacity:
            return self._buf[start:end].copy(), i0
        split = self.capacity - start
        out = np.empty(n, dtype=np.complex64)
        out[:split] = self._buf[start:]
        out[split:] = self._buf[: n - split]
        return out, i0


def extract_window(ring: IQRingBuffer, ping, sample_rate_hz: float,
                   pre_roll_s: float, post_roll_s: float,
                   max_window_s: float = 0.0):
    """Return (iq, first_abs) for pre-roll + event + post-roll around a ping.

    `max_window_s` > 0 caps the total window length (v1.4). A 2 s aircraft-ish
    ping otherwise yields a 3 s / ~25 MB window, and that window is what a
    queued snapshot job holds in memory. The pre-roll and the ping's onset are
    what matter for triage, so the cap trims the TAIL.
    """
    pre = int(pre_roll_s * sample_rate_hz)
    post = int(post_roll_s * sample_rate_hz)
    start = ping.start_sample - pre
    stop = ping.end_sample + post
    if max_window_s > 0:
        stop = min(stop, start + int(max_window_s * sample_rate_hz))
    return ring.get(start, stop)


def _axes(iq_len: int, first_abs: int, ping, sample_rate_hz: float,
          center_hz: float, nfft: int, row_decim: int):
    """Frequency (MHz) and time (ms, relative to ping start) axes for a window."""
    baseband = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate_hz))
    freqs_mhz = (center_hz + baseband) / 1e6
    row_dt_ms = nfft / sample_rate_hz * 1e3 * row_decim
    t0_ms = (first_abs - ping.start_sample) / sample_rate_hz * 1e3
    n_rows = -(-dsp.n_rows_for(iq_len, nfft) // row_decim)
    times_ms = t0_ms + np.arange(n_rows) * row_dt_ms
    return freqs_mhz, times_ms


def _png_path(out_dir: str, ping, label: str | None) -> str:
    stamp = ping.start.strftime("%Y%m%dT%H%M%S_%f")[:-3]
    suffix = f"_{label}" if label else ""
    return os.path.join(out_dir, f"ping_{stamp}{suffix}.png")


def render_png(
    sxx: np.ndarray,
    freqs_mhz: np.ndarray,
    times_ms: np.ndarray,
    png_path: str,
    *,
    title: str,
    target_hz: float,
    detect_halfwidth_hz: float,
    fig_w: float = 9.0,
    fig_h: float = 6.0,
    dpi: int = 110,
) -> str:
    """Draw one spectrogram PNG. Shared by the live renderer and the offline
    render_snapshots.py, so a Pi capture and a PC re-render look identical.

    matplotlib is imported HERE, not at module scope, so `snapshot_mode="npz"`
    (the Pi) never pays its ~90 MB of RSS. Uses the object-oriented Figure API
    (not pyplot) so rendering is thread-safe on the background worker.
    """
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(fig_w, fig_h))
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1)
    vmin, vmax = np.percentile(sxx, [5, 99.5])
    im = ax.imshow(
        sxx, aspect="auto", origin="lower", cmap="magma",
        extent=[freqs_mhz[0], freqs_mhz[-1], times_ms[0], times_ms[-1]],
        vmin=vmin, vmax=vmax, interpolation="nearest",
    )
    # Mark the target channel and detection band.
    ax.axvline(target_hz / 1e6, color="cyan", ls="--", lw=0.8, alpha=0.8)
    lo = (target_hz - detect_halfwidth_hz) / 1e6
    hi = (target_hz + detect_halfwidth_hz) / 1e6
    ax.axvline(lo, color="cyan", ls=":", lw=0.6, alpha=0.5)
    ax.axvline(hi, color="cyan", ls=":", lw=0.6, alpha=0.5)
    ax.axhline(0.0, color="white", ls="-", lw=0.5, alpha=0.4)  # ping start

    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Time relative to ping start (ms)")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="Power (dB)", pad=0.01)
    fig.tight_layout()
    fig.savefig(png_path, dpi=dpi)
    return png_path


def _title(ping, target_hz: float, label: str | None) -> str:
    label_txt = f"  |  {label.upper()}" if label else ""
    return (
        f"Ping {ping.start.strftime('%H:%M:%S')} UTC  |  "
        f"{ping.duration_s*1e3:.0f} ms  |  SNR {ping.peak_snr_db:.1f} dB{label_txt}\n"
        f"cyan dashed = {target_hz/1e6:.3f} MHz target channel"
    )


def save_snapshot(
    iq: np.ndarray,
    first_abs: int,
    ping,
    *,
    sample_rate_hz: float,
    center_hz: float,
    target_hz: float,
    detect_halfwidth_hz: float,
    out_dir: str,
    nfft: int,
    label: str | None = None,
    save_raw_iq: bool = False,
    max_rows: int = 0,
    fig_w: float = 9.0,
    fig_h: float = 6.0,
    dpi: int = 110,
) -> str | None:
    """Render a spectrogram PNG for one ping's IQ window. Returns path or None."""
    os.makedirs(out_dir, exist_ok=True)
    if len(iq) < nfft:
        return None  # not enough retained context to render

    sxx, decim = dsp.spectrogram_db(iq, nfft, max_rows=max_rows)
    freqs_mhz, times_ms = _axes(len(iq), first_abs, ping, sample_rate_hz,
                                center_hz, nfft, decim)
    png_path = _png_path(out_dir, ping, label)
    render_png(sxx, freqs_mhz, times_ms, png_path,
               title=_title(ping, target_hz, label),
               target_hz=target_hz, detect_halfwidth_hz=detect_halfwidth_hz,
               fig_w=fig_w, fig_h=fig_h, dpi=dpi)

    if save_raw_iq:
        stamp = ping.start.strftime("%Y%m%dT%H%M%S_%f")[:-3]
        np.save(os.path.join(out_dir, f"ping_{stamp}.npy"), iq)

    return png_path


def save_npz(
    iq: np.ndarray,
    first_abs: int,
    ping,
    *,
    sample_rate_hz: float,
    center_hz: float,
    target_hz: float,
    detect_halfwidth_hz: float,
    out_dir: str,
    nfft: int,
    label: str | None = None,
    max_rows: int = 0,
) -> str | None:
    """Save the event as a compact, already-decimated float16 spectrogram (v1.4).

    This is the Pi path: all the information a PNG would show, at ~1-2 MB, with
    no matplotlib and no multi-second render. `render_snapshots.py` turns these
    into PNGs on a real machine later.

    float16 is plenty: the values are dB (roughly -120..0), where float16's ~3
    decimal digits give better than 0.1 dB resolution -- far finer than the
    colormap can show.
    """
    os.makedirs(out_dir, exist_ok=True)
    if len(iq) < nfft:
        return None

    sxx, decim = dsp.spectrogram_db(iq, nfft, max_rows=max_rows)
    freqs_mhz, times_ms = _axes(len(iq), first_abs, ping, sample_rate_hz,
                                center_hz, nfft, decim)
    stamp = ping.start.strftime("%Y%m%dT%H%M%S_%f")[:-3]
    suffix = f"_{label}" if label else ""
    path = os.path.join(out_dir, f"ping_{stamp}{suffix}.npz")

    np.savez_compressed(
        path,
        sxx=sxx.astype(np.float16),
        freqs_mhz=freqs_mhz.astype(np.float32),
        times_ms=times_ms.astype(np.float32),
        # Everything render_snapshots.py needs to redraw the exact same figure.
        start_utc=np.array(ping.start.isoformat()),
        duration_s=np.float64(ping.duration_s),
        peak_snr_db=np.float64(ping.peak_snr_db),
        peak_power_db=np.float64(ping.peak_power_db),
        target_hz=np.float64(target_hz),
        center_hz=np.float64(center_hz),
        detect_halfwidth_hz=np.float64(detect_halfwidth_hz),
        label=np.array(label or ""),
    )
    return path


def write_snapshot(cfg, job) -> str | None:
    """Write one event artifact in whatever mode `cfg.snapshot_mode` selects.

    The single place that maps a mode to a writer, shared by the async worker
    and the synchronous fallback path.
    """
    common = dict(
        sample_rate_hz=cfg.sample_rate_hz,
        center_hz=job.center_hz, target_hz=job.target_hz,
        detect_halfwidth_hz=cfg.detect_halfwidth_hz,
        out_dir=cfg.snapshot_dir, nfft=cfg.snapshot_nfft,
        label=job.label, max_rows=cfg.snapshot_max_rows,
    )
    if cfg.snapshot_mode == "npz":
        return save_npz(job.iq, job.first_abs, job.ping, **common)
    if cfg.snapshot_mode == "png":
        return save_snapshot(
            job.iq, job.first_abs, job.ping,
            save_raw_iq=cfg.snapshot_save_raw_iq,
            fig_w=cfg.snapshot_fig_w, fig_h=cfg.snapshot_fig_h,
            dpi=cfg.snapshot_fig_dpi, **common)
    return None  # "off"


@dataclass
class SnapshotJob:
    iq: np.ndarray
    first_abs: int
    ping: object
    center_hz: float
    target_hz: float
    label: str
    index: int = -1        # caller's event index (for GUI to attach the thumbnail)

    @property
    def nbytes(self) -> int:
        return int(self.iq.nbytes)


class SnapshotWorker:
    """Writes snapshots on a background thread so rendering never stalls the
    capture read.

    Backpressure is by BYTES (v1.4). Jobs carry an IQ window whose size varies
    with event duration -- from ~4 MB for a brief meteor to ~25 MB for a long
    one -- so bounding the queue at N jobs does not bound memory at all. We
    instead track queued bytes and DROP a job that would push us over
    `snapshot_queue_max_bytes` (the event is still logged and classified; only
    its image is lost). `snapshot_queue_max` remains as a secondary count cap.
    """

    def __init__(self, cfg, on_saved=None, on_drop=None) -> None:
        self.cfg = cfg
        self.on_saved = on_saved     # (path, index) -- called from worker thread
        self.on_drop = on_drop       # (dropped_total)
        self.mode = getattr(cfg, "snapshot_mode", "png")
        self.max_bytes = int(getattr(cfg, "snapshot_queue_max_bytes",
                                     64 * 1024 * 1024))
        self.q: "queue.Queue" = queue.Queue(maxsize=max(1, cfg.snapshot_queue_max))
        self._lock = threading.Lock()
        self._queued_bytes = 0
        self.peak_queued_bytes = 0
        self.dropped = 0
        self.saved = 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._queued_bytes

    def _drop(self) -> bool:
        self.dropped += 1
        if self.on_drop:
            self.on_drop(self.dropped)
        return False

    def submit(self, job: SnapshotJob) -> bool:
        nbytes = job.nbytes
        with self._lock:
            if self._queued_bytes + nbytes > self.max_bytes:
                return self._drop()
            self._queued_bytes += nbytes
            self.peak_queued_bytes = max(self.peak_queued_bytes,
                                         self._queued_bytes)
        try:
            self.q.put_nowait(job)
            return True
        except queue.Full:
            with self._lock:
                self._queued_bytes -= nbytes
            return self._drop()

    def _run(self) -> None:
        while True:
            job = self.q.get()
            if job is None:              # sentinel -> shut down
                self.q.task_done()
                break
            try:
                path = write_snapshot(self.cfg, job)
                if path:
                    self.saved += 1
                    if self.on_saved:
                        self.on_saved(path, job.index)
            except Exception:  # noqa: BLE001 (never let a render kill the worker)
                pass
            finally:
                nbytes = job.nbytes
                job.iq = None            # release the window before the next job
                with self._lock:
                    self._queued_bytes = max(0, self._queued_bytes - nbytes)
                self.q.task_done()

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.q.put_nowait(None)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass
        self._t.join(timeout=timeout)
