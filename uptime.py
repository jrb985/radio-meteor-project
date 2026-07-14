"""Observing-exposure log (v1.5) -- when was the receiver actually listening?

Every rate in this project is counts / EXPOSURE, so exposure has to be measured,
not guessed. Before this module, diurnal.py inferred it from the span of logged
events ("the receiver was on whenever anything was logged"). That is a trap:

    Run only 22:00-06:00 for a week. The first-to-last-event span covers the
    whole week INCLUDING the daytime hours you never observed, so every
    hour-of-day bin gets ~7 h of "exposure" -- but the daytime bins hold almost
    no counts. Rate = counts / exposure then collapses the daytime and keeps the
    night high, producing a textbook-looking diurnal curve (peak ~06 h, trough
    ~18 h) that is PURELY an artifact of when you switched the radio on.

That failure mode is indistinguishable from success, which makes it the worst
kind. So the detector now records what it actually did.

FORMAT -- `sessions.csv`, append-only, one event per row:

    utc,event,note
    2026-07-14T04:12:03+00:00,on,warmup complete
    2026-07-14T04:13:03+00:00,beat,
    ...
    2026-07-14T09:31:44+00:00,off,reconnect
    2026-07-14T09:31:58+00:00,on,reconnected
    2026-07-14T14:02:11+00:00,off,stop

Append-only is deliberate: a power cut cannot corrupt what is already written.
`beat` rows are what make a crash survivable -- if the process dies without an
`off`, the interval is closed at the last beat, so exposure is under-counted by
at most one beat interval (60 s) instead of losing the whole session or, worse,
counting the dead hours as observed.

Exposure EXCLUDES: warm-up (the detector is not yet detecting) and USB
reconnect gaps (it is not listening).
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import time

HOUR = dt.timedelta(hours=1)
HEADER = ["utc", "event", "note"]


class UptimeLog:
    """Append-only writer. Cheap: one short line per minute."""

    def __init__(self, path: str, beat_s: float = 60.0, enabled: bool = True) -> None:
        self.path = path
        self.beat_s = float(beat_s)
        self.enabled = enabled
        self._open = False
        self._last_beat = 0.0
        self._f = None
        self._w = None
        if not enabled:
            return

        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        if new:
            self._w.writerow(HEADER)
            self._f.flush()

    def _write(self, event: str, note: str = "") -> None:
        if not self.enabled:
            return
        self._w.writerow([dt.datetime.now(dt.timezone.utc).isoformat(), event, note])
        self._f.flush()          # flush every row: a crash must not lose the tail
        os.fsync(self._f.fileno())

    def on(self, note: str = "") -> None:
        """Observing started (warm-up done, or resumed after a reconnect)."""
        if self._open:
            return
        self._open = True
        self._last_beat = time.time()
        self._write("on", note)

    def beat(self) -> None:
        """Call freely from the read loop; throttles itself to beat_s."""
        if not self._open:
            return
        now = time.time()
        if now - self._last_beat >= self.beat_s:
            self._last_beat = now
            self._write("beat")

    def off(self, note: str = "") -> None:
        """Observing stopped (clean shutdown, or a USB gap opened)."""
        if not self._open:
            return
        self._open = False
        self._write("off", note)

    def close(self, note: str = "stop") -> None:
        self.off(note)
        if self._f is not None:
            self._f.close()
            self._f = None


# ----------------------------------------------------------------- reading
def load_intervals(path: str) -> list[tuple[dt.datetime, dt.datetime]]:
    """Reconstruct the closed observing intervals from the event log.

    Handles the messy cases explicitly:
      * `on` with no matching `off` (the process was killed) -> close at the last
        beat, NOT at "now" and NOT at the next session's start.
      * a stray second `on` -> close the previous interval first.
      * `off` without an open interval -> ignore.
    """
    if not os.path.exists(path):
        return []

    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    start: dt.datetime | None = None
    last: dt.datetime | None = None

    def close(end: dt.datetime | None) -> None:
        nonlocal start, last
        if start is not None and end is not None and end > start:
            intervals.append((start, end))
        start = last = None

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                t = dt.datetime.fromisoformat(row["utc"])
            except (ValueError, KeyError, TypeError):
                continue
            event = (row.get("event") or "").strip()
            if event == "on":
                close(last)              # unterminated previous run
                start = last = t
            elif event == "beat":
                if start is not None:
                    last = t
            elif event == "off":
                if start is not None:
                    close(t)

    close(last)                          # file ended mid-session (crash)
    return intervals


def exposure_by_local_hour(intervals) -> list[float]:
    """Fractional observing hours per LOCAL hour-of-day (24 bins).

    Integrates each interval across the hour boundaries it straddles, so a run
    from 03:40 to 05:10 contributes 0.33 h to bin 3, 1.0 h to bin 4, and 0.17 h
    to bin 5 -- rather than being dumped whole into one bin.
    """
    exp = [0.0] * 24
    for t0, t1 in intervals:
        t0, t1 = t0.astimezone(), t1.astimezone()   # UTC -> local
        slot = t0.replace(minute=0, second=0, microsecond=0)
        while slot < t1:
            nxt = slot + HOUR
            covered = (min(nxt, t1) - max(slot, t0)).total_seconds() / 3600.0
            if covered > 0:
                exp[slot.hour] += covered
            slot = nxt
    return exp


def total_hours(intervals) -> float:
    return sum((b - a).total_seconds() for a, b in intervals) / 3600.0
