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
import sys
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


def memory() -> str:
    """Free RAM and what the detector is using -- the number that matters on a
    1 GB Pi. Reads /proc directly; psutil is not a dependency."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024   # kB -> bytes
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used_pct = 100.0 * (1 - avail / total) if total else 0.0
        out = (f"RAM {avail/1e6:.0f} MB free of {total/1e6:.0f} MB "
               f"({used_pct:.0f}% used)")
    except OSError:
        return ""

    # RSS of the detector process, if it is running.
    for statm in glob.glob("/proc/[0-9]*/statm"):
        try:
            with open(os.path.join(os.path.dirname(statm), "cmdline"), "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
            if "meteor_detector.py" not in cmd:
                continue
            with open(statm) as f:
                rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            out += f" &middot; detector RSS <b>{rss/1e6:.0f} MB</b>"
            break
        except (OSError, IndexError, ValueError):
            continue
    return out


def page() -> str:
    total, by, last = counts()
    p = ["<h1>Radio Meteor Tracker</h1>",
         f"<p><b>Total events:</b> {total} &nbsp;&nbsp;" +
         " ".join(f"<b>{html.escape(k)}</b>={v}" for k, v in sorted(by.items())) +
         "</p>"]
    mem = memory()
    if mem:
        p.append(f"<p style='color:#555'>{mem}</p>")
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
