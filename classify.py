"""Heuristic event classification (v1.2).

Triage each detected ping from its IQ window into:
  * meteor       -- brief, energy concentrated IN the target channel, little
                    frequency drift.
  * aircraft     -- energy in-channel but the carrier DRIFTS in frequency
                    (Doppler) and/or the event is comparatively long.
  * interference -- energy is SPREAD across the whole sampled span, not
                    localized to the channel (broadband / impulsive).

This is a transparent rule-based classifier (no training data yet); every
threshold is in config.py. It returns the label plus the features it used so
results can be inspected and the thresholds tuned.

Runs INLINE on the capture thread (the label has to be ready for the CSV row),
so it must be cheap in both time and memory -- it reduces the event to per-row
scalars via dsp.band_stats rather than building a full spectrogram (v1.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

import dsp


@dataclass
class Classification:
    label: str
    confidence: float
    features: dict = field(default_factory=dict)


def classify_event(
    iq: np.ndarray,
    ping,
    first_abs: int,
    *,
    sample_rate_hz: float,
    center_hz: float,
    target_hz: float,
    detect_halfwidth_hz: float,
    nfft: int,
    inband_frac_min: float,
    aircraft_min_ms: float,
    drift_hz: float,
) -> Classification:
    """Classify one event. `first_abs` is the absolute index of iq[0]."""
    n_rows = dsp.n_rows_for(len(iq), nfft)
    if n_rows < 1:
        return Classification("unknown", 0.0, {"reason": "too short"})

    baseband = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate_hz))
    abs_freq = center_hz + baseband
    band = np.abs(abs_freq - target_hz) <= detect_halfwidth_hz
    f_band = abs_freq[band]

    # Per-row reductions only -- the full (n_rows, nfft) spectrogram is never
    # materialized (see dsp.band_stats).
    row_total, row_inband, row_centroid = dsp.band_stats(iq, nfft, band, f_band)

    # Which rows belong to the event (0..duration relative to ping start)?
    row_dt = nfft / sample_rate_hz
    t0 = (first_abs - ping.start_sample) / sample_rate_hz
    times = t0 + np.arange(n_rows) * row_dt
    ev = (times >= 0.0) & (times <= ping.duration_s)
    if ev.sum() < 1:
        ev = np.zeros(n_rows, dtype=bool)
        ev[int(row_total.argmax())] = True  # fall back to peak row

    total_energy = float(row_total[ev].sum()) + 1e-12
    inband_energy = float(row_inband[ev].sum())
    in_band_frac = inband_energy / total_energy

    # In-band spectral centroid per event row -> linear drift over the event.
    centroid = row_centroid[ev]
    ev_times = times[ev]
    if len(ev_times) >= 2 and np.ptp(ev_times) > 0:
        slope = np.polyfit(ev_times - ev_times[0], centroid, 1)[0]  # Hz/s
        total_drift = float(slope * (ev_times[-1] - ev_times[0]))
    else:
        total_drift = 0.0

    dur_ms = ping.duration_s * 1000.0
    feats = {
        "in_band_frac": round(in_band_frac, 3),
        "drift_hz": round(total_drift, 1),
        "duration_ms": round(dur_ms, 1),
    }

    if in_band_frac < inband_frac_min:
        conf = min(1.0, (inband_frac_min - in_band_frac) / max(inband_frac_min, 1e-6))
        return Classification("interference", round(conf, 2), feats)
    if dur_ms >= aircraft_min_ms or abs(total_drift) >= drift_hz:
        return Classification("aircraft", 0.6, feats)
    # Concentrated, brief, low-drift -> meteor. Confidence grows with in_band_frac.
    conf = min(1.0, (in_band_frac - inband_frac_min) / (1.0 - inband_frac_min))
    return Classification("meteor", round(max(0.3, conf), 2), feats)


def classify_with_config(iq, ping, first_abs, cfg, *, center_hz, target_hz) -> Classification:
    """Convenience wrapper pulling thresholds from a Config."""
    return classify_event(
        iq, ping, first_abs,
        sample_rate_hz=cfg.sample_rate_hz,
        center_hz=center_hz,
        target_hz=target_hz,
        detect_halfwidth_hz=cfg.detect_halfwidth_hz,
        nfft=cfg.snapshot_nfft,
        inband_frac_min=cfg.classify_inband_frac_min,
        aircraft_min_ms=cfg.classify_aircraft_min_ms,
        drift_hz=cfg.classify_drift_hz,
    )
