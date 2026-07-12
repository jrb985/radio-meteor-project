"""Render the Pi's compact .npz snapshots into PNGs (v1.4).

With `snapshot_mode="npz"` (the Raspberry Pi profile) the detector saves each
event as a small float16 spectrogram instead of rendering an image: no
matplotlib on the Pi, no seconds-long render on a slow CPU, ~1-2 MB per event
instead of a PNG plus the memory spike to make it. Run this afterwards -- on the
Pi, or more usefully on your PC over a copied `snapshots/` folder -- to turn
them into the same images the PNG mode would have produced.

    python render_snapshots.py                     # ./snapshots, skip existing
    python render_snapshots.py --dir D:\\pi_snaps   # a folder copied off the Pi
    python render_snapshots.py --force             # re-render everything

Copy the folder off the Pi with e.g.:
    scp -r pi@meteorpi:~/radio_metor_tracker/snapshots D:\\pi_snaps
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np

from config import CONFIG
from snapshot import render_png


def render_one(npz_path: str, force: bool = False) -> str | None:
    """Render one .npz to a PNG beside it. Returns the path, or None if skipped."""
    png_path = os.path.splitext(npz_path)[0] + ".png"
    if os.path.exists(png_path) and not force:
        return None

    with np.load(npz_path, allow_pickle=False) as d:
        # Stored float16 to keep the Pi's files small; matplotlib wants >=float32.
        sxx = d["sxx"].astype(np.float32)
        freqs_mhz = d["freqs_mhz"]
        times_ms = d["times_ms"]
        label = str(d["label"])
        start_utc = str(d["start_utc"])
        duration_s = float(d["duration_s"])
        peak_snr_db = float(d["peak_snr_db"])
        target_hz = float(d["target_hz"])
        halfwidth = float(d["detect_halfwidth_hz"])

    # Same title layout as the live PNG path, rebuilt from the stored metadata.
    hhmmss = start_utc.split("T")[-1][:8] if "T" in start_utc else start_utc
    label_txt = f"  |  {label.upper()}" if label else ""
    title = (
        f"Ping {hhmmss} UTC  |  {duration_s*1e3:.0f} ms  |  "
        f"SNR {peak_snr_db:.1f} dB{label_txt}\n"
        f"cyan dashed = {target_hz/1e6:.3f} MHz target channel"
    )
    return render_png(
        sxx, freqs_mhz, times_ms, png_path,
        title=title, target_hz=target_hz, detect_halfwidth_hz=halfwidth,
        fig_w=CONFIG.snapshot_fig_w, fig_h=CONFIG.snapshot_fig_h,
        dpi=CONFIG.snapshot_fig_dpi)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", default=CONFIG.snapshot_dir,
                   help="folder of .npz snapshots (default: %(default)s)")
    p.add_argument("--force", action="store_true",
                   help="re-render even if the PNG already exists")
    args = p.parse_args(argv)

    files = sorted(glob.glob(os.path.join(args.dir, "*.npz")))
    if not files:
        print(f"No .npz snapshots in {args.dir!r}.")
        return 1

    rendered = skipped = failed = 0
    for i, f in enumerate(files, 1):
        try:
            out = render_one(f, force=args.force)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(files)}] FAILED {os.path.basename(f)}: {e}")
            continue
        if out is None:
            skipped += 1
        else:
            rendered += 1
            print(f"  [{i}/{len(files)}] {os.path.basename(out)}")

    print(f"\n{rendered} rendered, {skipped} already present, {failed} failed "
          f"-> {args.dir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
