"""Session report (v1.2): summarize a night of meteor_events.csv.

Produces a pings-per-hour chart (stacked by classification) plus a text
summary -- designed for reviewing a meteor-shower observing session.

    python session_report.py                    # reads meteor_events.csv
    python session_report.py events.csv report.png
"""
from __future__ import annotations

import csv
import sys
import datetime as dt
from collections import defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CLASS_ORDER = ["meteor", "aircraft", "interference", "unknown", ""]
CLASS_COLOR = {
    "meteor": "#2ca02c", "aircraft": "#ff7f0e",
    "interference": "#888888", "unknown": "#1f77b4", "": "#1f77b4",
}
CLASS_LABEL = {"": "unclassified"}


def _load(path: str):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                t = dt.datetime.fromisoformat(r["start_utc"]).astimezone()
            except (ValueError, KeyError):
                continue
            rows.append({
                "t": t,
                "duration_s": float(r.get("duration_s", 0) or 0),
                "snr": float(r.get("peak_snr_db", 0) or 0),
                "cls": (r.get("classification") or "").strip(),
            })
    return rows


def _summary(rows):
    by_class = defaultdict(int)
    for r in rows:
        by_class[r["cls"] or "unclassified"] += 1
    span = (rows[-1]["t"] - rows[0]["t"]) if len(rows) > 1 else dt.timedelta(0)
    hours = max(span.total_seconds() / 3600.0, 1e-9)
    meteors = by_class.get("meteor", 0)
    lines = [
        f"Session report  ({len(rows)} events)",
        f"  Span      : {rows[0]['t']:%Y-%m-%d %H:%M} -> {rows[-1]['t']:%H:%M} "
        f"local  ({span})",
        f"  Meteors   : {meteors}  (~{meteors / hours:.1f}/hr)",
    ]
    for cls in ["aircraft", "interference", "unclassified"]:
        if by_class.get(cls):
            lines.append(f"  {cls.capitalize():10s}: {by_class[cls]}")
    return "\n".join(lines), by_class


def main(argv):
    csv_path = argv[0] if argv else "meteor_events.csv"
    out_png = argv[1] if len(argv) > 1 else "session_report.png"

    try:
        rows = _load(csv_path)
    except FileNotFoundError:
        print(f"No such file: {csv_path}")
        return 2
    if not rows:
        print(f"No events found in {csv_path}")
        return 1

    rows.sort(key=lambda r: r["t"])
    summary_txt, _ = _summary(rows)
    print(summary_txt)

    # Bucket counts by local clock hour and class.
    per_hour = defaultdict(lambda: defaultdict(int))
    for r in rows:
        hour = r["t"].replace(minute=0, second=0, microsecond=0)
        per_hour[hour][r["cls"] or ""] += 1
    hours = sorted(per_hour)
    # Fill any gap hours so the timeline is continuous.
    full = []
    h = hours[0]
    while h <= hours[-1]:
        full.append(h); h += dt.timedelta(hours=1)

    classes = [c for c in CLASS_ORDER if any(per_hour[hr].get(c) for hr in hours)]
    x = np.arange(len(full))
    bottom = np.zeros(len(full))

    fig, ax = plt.subplots(figsize=(11, 5))
    for c in classes:
        vals = np.array([per_hour[hr].get(c, 0) for hr in full], dtype=float)
        ax.bar(x, vals, bottom=bottom, width=0.85,
               color=CLASS_COLOR.get(c, "#1f77b4"),
               label=CLASS_LABEL.get(c, c or "unclassified"))
        bottom += vals

    ax.set_xticks(x)
    ax.set_xticklabels([hr.strftime("%H:%M") for hr in full], rotation=45, fontsize=8)
    ax.set_xlabel("Local hour")
    ax.set_ylabel("Events per hour")
    ax.set_title(f"Observing session -- {rows[0]['t']:%Y-%m-%d}  "
                 f"({len(rows)} events)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.text(0.99, 0.02, summary_txt, ha="right", va="bottom",
             family="monospace", fontsize=8,
             bbox=dict(boxstyle="round", fc="#f5f5f5", ec="#ccc"))
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    print(f"Saved {out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
