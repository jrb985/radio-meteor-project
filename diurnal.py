"""Diurnal analysis (v1.2 science tool): meteor rate vs local hour-of-day.

Folds many days of meteor_events.csv onto a single 24-hour axis to reveal the
sporadic diurnal rhythm (rate rises toward a ~06h local maximum, dips near ~18h).
Counts are normalized by observing EXPOSURE per hour-of-day, giving meteors/hour
with Poisson error bars.

    python diurnal.py                      # meteor-only, reads meteor_events.csv
    python diurnal.py events.csv out.png   # explicit paths
    python diurnal.py --all                # include every class (not just meteor)
    python diurnal.py --class aircraft     # some other single class
    python diurnal.py --sessions path.csv  # explicit exposure log

EXPOSURE (v1.5). Exposure is now MEASURED, from the sessions.csv the detector
writes (see uptime.py) -- the hours it was genuinely detecting, excluding warm-up
and USB reconnect gaps. Up to v1.4 it was *inferred* from the span of logged
events, which quietly fabricates the result on any non-continuous schedule: a
night-only run makes the unobserved daytime hours look observed-but-empty,
crushing their rate and producing a textbook-perfect diurnal curve out of nothing
but the observing schedule. If sessions.csv is absent this still falls back to the
old estimate, but says so in the loudest terms it can.
"""
from __future__ import annotations

import csv
import sys
import datetime as dt

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import uptime
from config import CONFIG

HOUR = dt.timedelta(hours=1)


def _load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                t = dt.datetime.fromisoformat(r["start_utc"]).astimezone()
            except (ValueError, KeyError):
                continue
            rows.append({"t": t, "cls": (r.get("classification") or "").strip()})
    return rows


def _exposure_hours(t0, t1):
    """Fractional observing hours per local hour-of-day over [t0, t1]."""
    exp = np.zeros(24)
    slot = t0.replace(minute=0, second=0, microsecond=0)
    while slot <= t1:
        slot_end = slot + HOUR
        covered = (min(slot_end, t1) - max(slot, t0)).total_seconds() / 3600.0
        if covered > 0:
            exp[slot.hour] += covered
        slot = slot_end
    return exp


def main(argv):
    flags = [a for a in argv if a.startswith("--")]
    pos = [a for a in argv if not a.startswith("--")]
    csv_path = pos[0] if pos else "meteor_events.csv"
    out_png = pos[1] if len(pos) > 1 else "diurnal.png"

    uptime_path = CONFIG.uptime_log
    for fl in flags:
        if fl.startswith("--sessions="):
            uptime_path = fl.split("=", 1)[1]

    want = "meteor"
    if "--all" in flags:
        want = None
    for fl in flags:
        if fl.startswith("--class"):
            # supports "--class=meteor" or "--class aircraft" (next positional)
            if "=" in fl:
                want = fl.split("=", 1)[1]
            elif len(pos) > (2 if len(pos) > 1 else 1):
                want = pos[-1]

    try:
        rows = _load(csv_path)
    except FileNotFoundError:
        print(f"No such file: {csv_path}")
        return 2
    if not rows:
        print(f"No events in {csv_path}")
        return 1

    rows.sort(key=lambda r: r["t"])
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    span_h = (t1 - t0).total_seconds() / 3600.0

    # --- Exposure -----------------------------------------------------------
    # MEASURED exposure (v1.5) from the detector's sessions.csv: the hours the
    # receiver was genuinely detecting, excluding warm-up and reconnect gaps.
    intervals = uptime.load_intervals(uptime_path)
    if intervals:
        exposure = np.array(uptime.exposure_by_local_hour(intervals))
        exp_src = f"measured ({uptime_path}, {len(intervals)} session(s))"
        exp_h = uptime.total_hours(intervals)
    else:
        # Fall back to the old guess -- but say so LOUDLY, because on a
        # night-only schedule this fabricates the very curve we are testing for.
        exposure = _exposure_hours(t0, t1)
        exp_src = "GUESSED from the event span -- see the warning below"
        exp_h = float(exposure.sum())

    sel = [r for r in rows if (want is None or r["cls"] == want)]
    counts = np.zeros(24)
    for r in sel:
        counts[r["t"].hour] += 1

    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(exposure > 0, counts / exposure, np.nan)
        err = np.where(exposure > 0, np.sqrt(counts) / exposure, np.nan)

    label = want if want else "all events"
    print(f"Diurnal analysis of '{label}' from {csv_path}")
    print(f"  Span     : {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M} local "
          f"({span_h:.1f} h, ~{span_h/24:.1f} days)")
    print(f"  Events   : {len(sel)} {label}  (of {len(rows)} total)")
    print(f"  Exposure : {exp_h:.1f} observing hours -- {exp_src}")
    covered = int((exposure > 0).sum())
    print(f"  Coverage : {covered}/24 local hours have any exposure")
    if np.nansum(rate) > 0:
        hmax = int(np.nanargmax(rate)); hmin = int(np.nanargmin(rate))
        print(f"  Peak     : {hmax:02d}h local ({rate[hmax]:.2f}/h)   "
              f"Min: {hmin:02d}h local ({rate[hmin]:.2f}/h)")
    if span_h < 24:
        print("  NOTE: < 24 h of data -- a real diurnal curve needs several days.")

    if not intervals:
        print(
            "\n  " + "!" * 68 + "\n"
            "  WARNING: no exposure log (sessions.csv) -- exposure was GUESSED from\n"
            "  the span of logged events, i.e. 'the receiver was on the whole time'.\n"
            "\n"
            "  If you did NOT observe continuously, this plot is an ARTIFACT. Running\n"
            "  only at night makes the daytime hours look observed-but-empty, which\n"
            "  crushes their rate and manufactures a textbook diurnal curve (peak ~06h,\n"
            "  trough ~18h) out of nothing but your own schedule. It will look like a\n"
            "  successful result. Do not trust it, and do not submit it.\n"
            "\n"
            "  Fix: run the detector (v1.5+) so it writes sessions.csv, then re-run.\n"
            "  " + "!" * 68)
    elif covered < 24:
        print(f"\n  NOTE: {24 - covered} local hour(s) have ZERO exposure -- they are\n"
              "  blank on the plot, not zero-rate. A trustworthy diurnal curve needs\n"
              "  coverage of the whole clock (run continuously, not only at night).")

    hours = np.arange(24)
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(hours, np.nan_to_num(rate), yerr=np.nan_to_num(err), width=0.8,
           color="#2ca02c", ecolor="#555", capsize=3, label=f"{label} rate")
    # reference markers for the expected sporadic extrema
    ax.axvline(6, color="tab:blue", ls="--", lw=1, alpha=0.6, label="~06h expected max")
    ax.axvline(18, color="tab:red", ls="--", lw=1, alpha=0.6, label="~18h expected min")
    # show thin exposure shading so low-coverage hours are obvious
    ax2 = ax.twinx()
    ax2.step(hours, exposure, where="mid", color="#bbb", lw=1)
    ax2.set_ylabel("Exposure (observing hours)", color="#999")
    ax2.set_ylim(bottom=0)

    ax.set_xticks(hours)
    ax.set_xlabel("Local hour of day")
    ax.set_ylabel("Rate (events / hour)")
    ax.set_title(f"Diurnal meteor rate ({label}) -- {t0:%Y-%m-%d} to {t1:%Y-%m-%d} "
                 f"({len(sel)} events, {exp_h:.0f} observing hours)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    # The PNG travels on its own (into a report, a post, an RMOB submission), so
    # the provenance of the exposure has to travel WITH it -- a viewer must never
    # have to know whether the terminal printed a warning.
    if intervals:
        fig.text(0.995, 0.015, f"exposure: measured from {uptime_path}",
                 ha="right", fontsize=7, color="#777")
    else:
        fig.text(0.5, 0.5,
                 "EXPOSURE GUESSED\nfrom event span -- may be an artifact",
                 ha="center", va="center", fontsize=26, color="red",
                 alpha=0.22, rotation=20, weight="bold", zorder=10)
        fig.text(0.995, 0.015,
                 "exposure GUESSED from event span (no sessions.csv) -- "
                 "not trustworthy unless observing was continuous",
                 ha="right", fontsize=7, color="red")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    print(f"Saved {out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
