"""Tunable parameters for the meteor forward-scatter detector.

Edit these to match your setup. The reference transmitter frequency is the
one thing you MUST get right -- it is the distant carrier a meteor trail
reflects to you.

Common references (pick per your location, then set REFERENCE_FREQ_HZ):
  * GRAVES radar (Europe)      143.050 MHz  -> 143_050_000
  * BRAMS beacon (Belgium/EU)   49.970 MHz  ->  49_970_000
  * Distant FM/TV carrier (NA)  varies       -> station carrier in Hz
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Config:
    # --- Reference signal -------------------------------------------------
    # Seattle meteor forward-scatter target: 104.3 MHz is locally VACANT but
    # carries KAWO Boise (52 kW, ~650 km) and KSOP-FM Salt Lake City (25 kW,
    # ~1100 km) -- both beyond the horizon, so normally silent. A meteor trail
    # reflects one of those FM signals to us as a brief broadband "ping".
    reference_freq_hz: float = 104_300_000.0

    # FM is ~180 kHz wide (not a CW carrier), so tune well off-channel to keep
    # the whole channel clear of the RTL-SDR DC spike, then watch its power.
    carrier_offset_hz: float = 250_000.0

    # --- Radio front-end --------------------------------------------------
    # Must span carrier_offset + detect_halfwidth; 1.024 MHz covers the whole
    # FM channel sitting at +250 kHz. (Valid RTL rates: 225k-300k, 900k-3.2M.)
    sample_rate_hz: float = 1_024_000.0
    # FIXED gain, not "auto": AGC hunts and its band-wide power swings look
    # like pings and make the baseline wander. 40.2 dB is a valid R820T step;
    # raise toward 49.6 for weak signals, lower if the front-end overloads.
    gain: str | float = 40.2
    freq_correction_ppm: int = 0               # set from rtl_test -p if known

    # --- Detection --------------------------------------------------------
    nfft: int = 4096                           # FFT size per analysis block (~4 ms)
    detect_halfwidth_hz: float = 90_000.0      # +/- band = full FM channel width
    snr_threshold_db: float = 4.0              # ping when band SNR exceeds this
    hysteresis_db: float = 3.0                 # must drop below (thr - this) to end
    min_ping_ms: float = 8.0                   # ignore blips shorter than this
    max_ping_ms: float = 2000.0                # longer = aircraft/tropo, not a meteor
    warmup_skip_s: float = 20.0                # settle tuner/baseline before detecting
    baseline_tau_s: float = 10.0               # noise-floor smoothing time constant

    # --- Output -----------------------------------------------------------
    log_csv: str = "meteor_events.csv"
    read_block_size: int = 256 * 1024          # IQ samples per SDR read

    # --- Trigger snapshots (v1.1) -----------------------------------------
    # On each detected ping, save a high-resolution spectrogram (and, if
    # enabled, the raw IQ) of the moment so events can be classified as
    # meteor / aircraft / interference after the fact.
    snapshot_enabled: bool = True
    snapshot_dir: str = "snapshots"
    snapshot_pre_roll_s: float = 0.5           # seconds of context before the ping
    snapshot_post_roll_s: float = 0.5          # seconds of context after the ping
    snapshot_nfft: int = 1024                  # fine FFT for the snapshot (~1 ms rows)
    snapshot_save_raw_iq: bool = False         # also dump raw IQ (.npy) per event

    # --- Event classification (v1.2) --------------------------------------
    # Heuristic triage of each ping from its IQ window. Tunable.
    classify_inband_frac_min: float = 0.45     # below => energy spread => interference
    classify_aircraft_min_ms: float = 800.0    # long in-band event => aircraft-like
    classify_drift_hz: float = 3000.0          # in-band centroid drift => aircraft-like

    # --- Async snapshots (v1.3) -------------------------------------------
    # Render snapshots on a background worker so matplotlib never stalls the
    # capture read (which would drop USB samples during event bursts).
    snapshot_async: bool = True
    snapshot_queue_max: int = 32               # drop renders (not events) if full
    snapshot_skip_aircraft: bool = False       # don't render aircraft-class events

    # --- Snapshot memory budget (v1.4) ------------------------------------
    # What the worker WRITES per event:
    #   "png" -- render a spectrogram image here (needs matplotlib; the GUI
    #            gallery reads these)
    #   "npz" -- save a decimated float16 spectrogram (~1-2 MB) and render it
    #            later on a PC with render_snapshots.py. matplotlib is then
    #            never imported by the capture process (~90 MB of RSS saved) and
    #            the multi-second render never happens. This is the Pi path.
    #   "off" -- classify + log only, no artifact
    snapshot_mode: str = "png"

    # A queued job holds a whole IQ window, and window size varies with event
    # duration (~4 MB for a brief meteor, ~25 MB for a 2 s one). So the queue
    # must be bounded in BYTES -- a job COUNT bounds nothing. Exceeding this
    # drops the snapshot, never the event (it is still classified and logged).
    snapshot_queue_max_bytes: int = 64 * 1024 * 1024
    # Cap the window length handed to the worker (0 = uncapped). Bounds the size
    # of any single job; the tail of a long event is trimmed, not its onset.
    snapshot_max_window_s: float = 0.0
    # Max spectrogram rows kept (0 = all). Rows above this are max-pooled -- a
    # ~1000 px figure cannot resolve 3000 rows anyway, and max (not mean)
    # preserves the brief bright streak that IS the ping.
    snapshot_max_rows: int = 0
    snapshot_fig_w: float = 9.0
    snapshot_fig_h: float = 6.0
    snapshot_fig_dpi: int = 110
    # Seconds of IQ retained by the ring (0 = derive from pre/post/max_ping).
    ring_span_s: float = 0.0

    # --- Auto-reconnect (v1.3) --------------------------------------------
    # Recover from USB/read errors instead of exiting -- essential for long
    # unattended runs (and the headless Pi service).
    reconnect_enabled: bool = True
    reconnect_backoff_start_s: float = 1.0
    reconnect_backoff_max_s: float = 30.0
    reconnect_give_up_after: int = 0           # 0 = never give up (keep retrying)

    # ---------------------------------------------------------------------
    def ring_span_seconds(self) -> float:
        """Seconds of IQ the ring must retain to serve any snapshot window."""
        if self.ring_span_s > 0:
            return self.ring_span_s
        event_s = (self.snapshot_max_window_s if self.snapshot_max_window_s > 0
                   else self.snapshot_pre_roll_s + self.max_ping_ms / 1000.0
                   + self.snapshot_post_roll_s)
        return event_s + 1.0   # +1 s slack: the ping is only handled after the
                               # read block that completed it has been processed

    def ring_capacity_samples(self) -> int:
        return int(self.ring_span_seconds() * self.sample_rate_hz)


# --- Profiles -------------------------------------------------------------
# A profile is a set of overrides for a specific machine. Select one with the
# METEOR_PROFILE environment variable or `--profile` on the CLI.
#
# "pi": Raspberry Pi 3B+ (1 GB RAM, shared with the OS and the USB stack).
# Measured peak RSS with these settings is ~120 MB, vs. an unbounded blow-up on
# the defaults -- see tools/mem_soak.py. The wins, in order of size:
#   snapshot_mode="npz"        no matplotlib in the capture process (~90 MB) and
#                              no seconds-long render on a slow CPU
#   snapshot_queue_max_bytes   hard ceiling on queued IQ windows (was ~790 MB
#                              worst case behind a 32-job bound)
#   snapshot_max_window_s      caps any one job at ~12 MB, and shrinks the ring
#   snapshot_skip_aircraft     near SeaTac these dominate; skipping them keeps
#                              the queue (and the SD card) free for meteors
PROFILES: dict[str, dict] = {
    "pi": dict(
        snapshot_mode="npz",
        snapshot_queue_max=4,
        snapshot_queue_max_bytes=48 * 1024 * 1024,
        snapshot_max_window_s=1.5,
        snapshot_max_rows=1024,
        snapshot_skip_aircraft=True,
        read_block_size=128 * 1024,
    ),
}


def get_config(profile: str | None = None) -> Config:
    """Build the Config, applying a named profile if one is selected.

    Precedence: explicit argument > METEOR_PROFILE env var > plain defaults.
    """
    name = (profile or os.environ.get("METEOR_PROFILE") or "").strip().lower()
    if not name or name == "default":
        return Config()
    if name not in PROFILES:
        raise ValueError(
            f"Unknown profile {name!r}. Known: {', '.join(sorted(PROFILES))}")
    return replace(Config(), **PROFILES[name])


CONFIG = get_config()   # honors METEOR_PROFILE (the Pi's systemd unit sets it)
