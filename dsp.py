"""Memory-lean spectral helpers shared by the classifier and the renderers (v1.4).

The naive way to spectrogram an event window is one big FFT:

    frames = iq.reshape(n_rows, nfft) * window
    p = np.abs(np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)) ** 2

That is what v1.3 did in both `classify.py` and `snapshot.py`, and it is fine on
a desktop -- but numpy's FFT always computes in DOUBLE precision, so every one of
`frames`, the FFT result, the fftshift copy, `abs` and the final log materializes
at full size in float64/complex128. A 3 s window at 1.024 Msps with nfft=1024 is
3000 x 1024, i.e. ~49 MB per complex128 intermediate -- a ~150-200 MB transient
spike for ONE event. On a 1 GB Pi 3B+, a burst of those is an OOM.

Everything here instead works in ROW BLOCKS: FFT a few hundred rows at a time,
immediately reduce to float32 (or to per-row scalars), and discard. Peak transient
becomes a few MB regardless of how long the event is, and the results are
numerically the same to float32 precision.
"""
from __future__ import annotations

import numpy as np

# Rows FFT'd at once. 256 x 1024 complex128 = 4 MB -- small enough for the Pi,
# large enough that the per-call numpy overhead stays amortized.
BLOCK_ROWS = 256


def n_rows_for(n_samples: int, nfft: int) -> int:
    """How many whole nfft-sized rows an IQ window yields."""
    return int(n_samples) // int(nfft)


def _row_blocks(iq: np.ndarray, nfft: int, n_rows: int, block_rows: int):
    """Yield (row0, power) for each block of rows.

    `power` is the fftshift-ed linear power spectrum, float32, shape
    (rows_in_block, nfft). The complex128 intermediates live only inside the
    loop body and are freed on the next iteration.
    """
    win = np.hanning(nfft).astype(np.float32)
    for row0 in range(0, n_rows, block_rows):
        rows = min(block_rows, n_rows - row0)
        chunk = iq[row0 * nfft : (row0 + rows) * nfft].reshape(rows, nfft) * win
        spec = np.fft.fftshift(np.fft.fft(chunk, axis=1), axes=1)
        # abs()**2 in one step, straight down to float32.
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        yield row0, power


def spectrogram_db(iq: np.ndarray, nfft: int, max_rows: int = 0,
                   block_rows: int = BLOCK_ROWS) -> tuple[np.ndarray, int]:
    """Spectrogram of `iq` in dB, float32, computed a block of rows at a time.

    If `max_rows` > 0 and the window has more rows than that, rows are MAX-POOLED
    down to at most `max_rows`. Max (not mean) because a meteor ping is a brief
    bright streak: averaging would dilute it into the noise floor, while max-pool
    preserves it. A 990-px-tall figure cannot resolve 3000 rows anyway.

    Returns (db, row_decim) where `row_decim` is the pooling factor, so the
    caller can build a correct time axis (row_dt *= row_decim).
    """
    nfft = int(nfft)
    n_rows = n_rows_for(len(iq), nfft)
    if n_rows < 1:
        return np.empty((0, nfft), dtype=np.float32), 1

    decim = 1
    if max_rows and n_rows > max_rows:
        decim = -(-n_rows // int(max_rows))  # ceil
    out_rows = -(-n_rows // decim)
    out = np.empty((out_rows, nfft), dtype=np.float32)

    # Keep row blocks aligned to the pooling groups so a group never straddles
    # two blocks.
    block_rows = max(decim, (block_rows // decim) * decim)

    for row0, power in _row_blocks(iq, nfft, n_rows, block_rows):
        db = 10.0 * np.log10(power + 1e-12, dtype=np.float32)
        if decim == 1:
            out[row0 : row0 + db.shape[0]] = db
        else:
            # Pad the final ragged group so the reshape-and-max works.
            rows = db.shape[0]
            groups = -(-rows // decim)
            if rows != groups * decim:
                pad = np.full((groups * decim - rows, nfft), -np.inf,
                              dtype=np.float32)
                db = np.concatenate([db, pad])
            o0 = row0 // decim
            out[o0 : o0 + groups] = db.reshape(groups, decim, nfft).max(axis=1)

    return out, decim


def band_stats(iq: np.ndarray, nfft: int, band_mask: np.ndarray,
               f_band: np.ndarray, block_rows: int = BLOCK_ROWS):
    """Per-row reductions the classifier needs, without ever holding the full
    (n_rows, nfft) spectrogram.

    Returns three float64 arrays of length n_rows:
        total     -- total power in the row (all bins)
        inband    -- power inside `band_mask`
        centroid  -- in-band spectral centroid (Hz), using `f_band` = the
                     absolute frequencies of the masked bins

    Memory is O(n_rows) instead of O(n_rows * nfft): a 3 s event drops from
    ~50 MB to ~72 KB.
    """
    nfft = int(nfft)
    n_rows = n_rows_for(len(iq), nfft)
    total = np.zeros(n_rows, dtype=np.float64)
    inband = np.zeros(n_rows, dtype=np.float64)
    centroid = np.zeros(n_rows, dtype=np.float64)
    if n_rows < 1:
        return total, inband, centroid

    for row0, power in _row_blocks(iq, nfft, n_rows, block_rows):
        rows = power.shape[0]
        sl = slice(row0, row0 + rows)
        pb = power[:, band_mask].astype(np.float64)   # (rows, n_band) -- small
        total[sl] = power.sum(axis=1, dtype=np.float64)
        row_pow = pb.sum(axis=1)
        inband[sl] = row_pow
        centroid[sl] = (pb * f_band).sum(axis=1) / (row_pow + 1e-12)

    return total, inband, centroid
