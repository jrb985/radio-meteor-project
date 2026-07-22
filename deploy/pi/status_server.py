"""Minimal headless status page for the meteor tracker (Python stdlib only).

    python deploy/pi/status_server.py [port]     # default 8080
Browse http://<pi-ip>:8080  -- shows total/by-class counts, the newest snapshot,
the report images, and the last few events. Auto-refreshes every 30 s.
"""
from __future__ import annotations

import csv
import glob
import html
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def counts():
    path = os.path.join(PROJ, "meteor_events.csv")
    total, by, rows = 0, {}, []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                total += 1
                c = (r.get("classification") or "?").strip() or "?"
                by[c] = by.get(c, 0) + 1
                rows.append(r)
    return total, by, rows[-12:][::-1]


def newest(pattern):
    fs = glob.glob(os.path.join(PROJ, pattern))
    return max(fs, key=os.path.getmtime) if fs else None


def _detector_rss() -> int | None:
    """RSS (bytes) of the running meteor_detector.py process, or None if not up."""
    for statm in glob.glob("/proc/[0-9]*/statm"):
        try:
            with open(os.path.join(os.path.dirname(statm), "cmdline"), "rb") as f:
                if "meteor_detector.py" not in f.read().decode("utf-8", "replace"):
                    continue
            with open(statm) as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            continue
    return None


def _reconnects() -> int | None:
    """How many times reconnect.py recovered a USB/read glitch this log -- the best
    software proxy for dongle health (RTL-SDR exposes no temperature sensor). Scans
    only the tail of meteor.log so it stays cheap as the log grows."""
    path = os.path.join(PROJ, "meteor.log")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 262144:
                f.seek(-262144, os.SEEK_END)
            tail = f.read().decode("utf-8", "replace")
        return tail.count("[reconnect]")
    except OSError:
        return None


def health():
    """Equipment/host health for a headless Pi, as a list of (label, value, level)
    with level in {'ok','warn','bad',''} driving color. Every probe is best-effort:
    a missing sensor yields no row rather than an error, so this also degrades
    cleanly when run off-Pi (e.g. rendering a test page on a PC)."""
    rows = []

    # SoC/CPU temperature. A bare 3B+ thermally throttles (~80-85 C) under sustained
    # DSP load; this is the host-side temperature that actually exists to read.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            c = int(f.read().strip()) / 1000.0
        lvl = "ok" if c < 70 else "warn" if c < 80 else "bad"
        rows.append(("CPU temp", f"{c:.1f} °C", lvl))
    except (OSError, ValueError):
        pass

    # Throttle / under-voltage flags. THE dongle-relevant signal: a marginal 5 V
    # supply trips under-voltage here first, then shows up as USB read errors.
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        val = int(out.split("=")[1], 16)                    # throttled=0x0
        flags = {0: "under-voltage now", 1: "ARM-freq capped now", 2: "throttled now",
                 16: "under-voltage since boot", 17: "freq-capped since boot",
                 18: "throttled since boot"}
        if val == 0:
            rows.append(("Power/throttle", "healthy (0x0)", "ok"))
        else:
            active = [m for bit, m in flags.items() if val & (1 << bit)]
            now = any(val & (1 << b) for b in (0, 1, 2))
            rows.append(("Power/throttle", f"0x{val:x}: " + ", ".join(active),
                         "bad" if now else "warn"))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass

    # Load average vs cores -- is the detector keeping up with the sample stream?
    try:
        with open("/proc/loadavg") as f:
            la1, la5, la15 = (float(x) for x in f.read().split()[:3])
        ncpu = os.cpu_count() or 1
        lvl = "ok" if la1 < ncpu else "warn" if la1 < ncpu * 1.5 else "bad"
        rows.append(("Load 1/5/15m",
                     f"{la1:.2f} / {la5:.2f} / {la15:.2f} ({ncpu} cores)", lvl))
    except (OSError, ValueError):
        pass

    # RAM free -- the number that matters on a 1 GB Pi. Reads /proc directly.
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024          # kB -> bytes
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        if total:
            used_pct = 100.0 * (1 - avail / total)
            lvl = "ok" if used_pct < 75 else "warn" if used_pct < 90 else "bad"
            rows.append(("RAM free",
                         f"{avail/1e6:.0f} MB of {total/1e6:.0f} MB ({used_pct:.0f}% used)",
                         lvl))
    except (OSError, ValueError):
        pass

    rss = _detector_rss()
    if rss is not None:
        rows.append(("Detector RSS", f"{rss/1e6:.0f} MB", "ok"))
    else:
        rows.append(("Detector", "NOT RUNNING", "bad"))

    # Free space on the card the project lives on -- a full card ends the run silently.
    try:
        st = os.statvfs(PROJ)
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        free_pct = 100.0 * free / total if total else 0.0
        lvl = "ok" if free_pct > 15 else "warn" if free_pct > 5 else "bad"
        rows.append(("Disk free",
                     f"{free/1e9:.1f} GB of {total/1e9:.1f} GB ({free_pct:.0f}%)", lvl))
    except (OSError, AttributeError):
        pass

    # System uptime.
    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        rows.append(("System uptime", f"{up//86400}d {up%86400//3600}h {up%3600//60}m", ""))
    except (OSError, ValueError):
        pass

    rc = _reconnects()
    if rc is not None:
        rows.append(("Dongle reconnects", str(rc), "ok" if rc == 0 else "warn"))

    return rows


_LEVEL_COLOR = {"ok": "#1a7f37", "warn": "#9a6700", "bad": "#cf222e", "": "#57606a"}


def health_html(rows) -> str:
    """Render health() rows as a responsive grid of color-dotted tiles."""
    if not rows:
        return ""
    cells = []
    for label, value, level in rows:
        dot = _LEVEL_COLOR.get(level, "#57606a")
        cells.append(
            "<div style='flex:1 1 210px;min-width:190px;padding:10px 12px;"
            "border:1px solid #d0d7de;border-radius:8px'>"
            f"<div style='font-size:11px;color:#57606a;text-transform:uppercase;"
            f"letter-spacing:.05em'>{html.escape(label)}</div>"
            "<div style='margin-top:4px'>"
            f"<span style='display:inline-block;width:9px;height:9px;border-radius:50%;"
            f"background:{dot};margin-right:7px;vertical-align:middle'></span>"
            f"<b style='vertical-align:middle'>{html.escape(value)}</b></div></div>")
    return ("<h3>Station health</h3>"
            "<div style='display:flex;flex-wrap:wrap;gap:10px'>" + "".join(cells)
            + "</div>")


def page() -> str:
    total, by, last = counts()
    p = ["<h1>Radio Meteor Tracker</h1>",
         f"<p><b>Total events:</b> {total} &nbsp;&nbsp;" +
         " ".join(f"<b>{html.escape(k)}</b>={v}" for k, v in sorted(by.items())) +
         "</p>"]
    p.append(health_html(health()))
    for title, fn in [("Session report", "session_report.png"),
                      ("Diurnal curve", "diurnal.png")]:
        if os.path.exists(os.path.join(PROJ, fn)):
            p.append(f"<h3>{title}</h3><img src='/file/{fn}' style='max-width:100%'>")

    ns = newest("snapshots/*.png")
    if ns:
        rel = os.path.relpath(ns, PROJ).replace(os.sep, "/")
        p.append(f"<h3>Newest snapshot</h3>"
                 f"<img src='/file/{html.escape(rel)}' style='max-width:100%'>")
    # The Pi profile saves .npz instead of rendering PNGs (snapshot_mode="npz"),
    # so there is usually nothing to show here -- report the backlog instead.
    n_npz = len(glob.glob(os.path.join(PROJ, "snapshots", "*.npz")))
    if n_npz:
        p.append(f"<h3>Snapshots</h3><p>{n_npz} unrendered <code>.npz</code> "
                 f"captures. Copy <code>snapshots/</code> to a PC and run "
                 f"<code>python render_snapshots.py</code> to view them.</p>")
    if last:
        p.append("<h3>Last events</h3><table border=1 cellpadding=4 "
                 "style='border-collapse:collapse'><tr><th>UTC</th><th>dur(s)</th>"
                 "<th>SNR</th><th>class</th></tr>")
        for r in last:
            p.append("<tr>" + "".join(
                f"<td>{html.escape(str(r.get(k, '')))}</td>"
                for k in ["start_utc", "duration_s", "peak_snr_db", "classification"]
            ) + "</tr>")
        p.append("</table>")
    return ("<html><head><meta http-equiv=refresh content=30>"
            "<title>Meteor Tracker</title></head>"
            "<body style='font-family:sans-serif;max-width:900px;margin:2em auto'>"
            + "".join(p) + "</body></html>")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path)
        if path.startswith("/file/"):
            full = os.path.normpath(os.path.join(PROJ, path[len("/file/"):]))
            # prevent path traversal outside the project dir
            if not full.startswith(PROJ) or not os.path.isfile(full):
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            with open(full, "rb") as f:
                self.wfile.write(f.read())
            return
        body = page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Serving on http://0.0.0.0:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
