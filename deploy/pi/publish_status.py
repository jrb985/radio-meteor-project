"""Render a self-contained docs/status.html from the live detector state, for
publishing to GitHub Pages (served at https://jrb985.github.io/radio-meteor-project/status.html).

Unlike status_server.py (which serves a live page on :8080 with /file/ image URLs
that only work while the server runs), this writes a SINGLE static file with no
external references, safe to commit and serve from Pages. It reuses the same data
functions so there is one source of truth for counts + health.

    python deploy/pi/publish_status.py [output.html]      # default: docs/status.html

Intended to be run hourly by deploy/pi/publish_status.sh (cron), which renders
this file and pushes it. See README_PI.md "Publishing a public status page".
"""
from __future__ import annotations

import html
import os
import sys
import time

# Reuse the live server's data + health logic (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from status_server import PROJ, counts, health, health_html  # noqa: E402

DEFAULT_OUT = os.path.join(PROJ, "docs", "status.html")


def render() -> str:
    total, by, last = counts()
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    by_str = " &nbsp; ".join(f"<b>{html.escape(k)}</b>={v}" for k, v in sorted(by.items()))
    body = [
        "<header>",
        "<h1>Radio Meteor Tracker</h1>",
        "<p class='sub'>FM forward-scatter meteor detector &middot; Seattle, WA &middot; "
        "Raspberry Pi station</p>",
        f"<p class='updated'>Updated {now} &middot; refreshes hourly</p>",
        "</header>",
        health_html(health()),
        "<h3>Detections</h3>",
        f"<p class='counts'><b>{total}</b> total events &nbsp; {by_str}</p>",
    ]

    if last:
        body.append("<h3>Latest events</h3><div class='tw'><table>"
                    "<tr><th>UTC</th><th>dur (s)</th><th>SNR (dB)</th><th>class</th></tr>")
        for r in last:
            body.append("<tr>" + "".join(
                f"<td>{html.escape(str(r.get(k, '')))}</td>"
                for k in ["start_utc", "duration_s", "peak_snr_db", "classification"]
            ) + "</tr>")
        body.append("</table></div>")
    else:
        body.append("<p class='counts'>No events logged yet.</p>")

    body.append("<footer><a href='index.html'>&larr; Project home</a> &middot; "
                "data updates hourly from the Pi; snapshots and the full event log "
                "stay on the station.</footer>")

    return _DOC.replace("__BODY__", "\n".join(body))


_DOC = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Station status - Radio Meteor Tracker</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='88'>&#9732;</text></svg>">
<style>
  :root{color-scheme:light dark}
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       max-width:920px;margin:0 auto;padding:2em 1.2em;line-height:1.5;
       color:#1f2328;background:#fff}
  h1{margin:0;font-size:1.6rem}
  h3{margin:1.8em 0 .6em;font-size:1rem;text-transform:uppercase;letter-spacing:.05em;
     color:#57606a;border-bottom:1px solid #d0d7de;padding-bottom:.3em}
  .sub{margin:.3em 0 0;color:#57606a}
  .updated{margin:.2em 0 0;color:#57606a;font-size:.85rem}
  .counts{font-size:1.05rem}
  .tw{overflow-x:auto}
  table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
  th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;font-size:.9rem}
  th{background:#f6f8fa}
  footer{margin-top:2.5em;padding-top:1em;border-top:1px solid #d0d7de;
         color:#57606a;font-size:.85rem}
  a{color:#0969da}
  @media (prefers-color-scheme:dark){
    body{color:#e6edf3;background:#0d1117}
    h3{color:#8b949e;border-color:#30363d}
    .sub,.updated,footer{color:#8b949e}
    th,td{border-color:#30363d}
    th{background:#161b22}
    a{color:#4493f8}
    /* health tiles carry inline light-mode borders; nudge them for dark */
    div[style*='border:1px solid #d0d7de']{border-color:#30363d !important}
  }
</style>
</head>
<body>
__BODY__
</body>
</html>
"""


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
