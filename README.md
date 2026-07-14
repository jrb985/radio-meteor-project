# Radio Meteor Project

Detecting meteors by **radio forward-scatter** with an RTL-SDR — no telescope, no
clear sky required.

The receiver listens on an FM channel that is *vacant* where I live (104.3 MHz in
Seattle). Normally there is nothing there. When a meteor burns up, its ionized
trail briefly reflects a *distant, over-the-horizon* FM transmitter — KAWO in
Boise, ~650 km away — down to the antenna. That momentary reflection is a
**"ping."** It works in daylight, and it works through the overcast that makes
visual meteor watching mostly hopeless here.

**📊 [Read the decks →](https://jrb985.github.io/radio-meteor-project/)** — architecture,
the physics, the science it can yield, antenna options, and long-run planning.

---

## What's in this repo

Everything needed to run the detector headless on a **Raspberry Pi 3B+**, plus
the published documentation.

```
config.py             All tunable parameters + PROFILES (e.g. the "pi" profile)
meteor_detector.py    The CLI detector: band-power -> baseline -> ping -> CSV
capture_engine.py     The same pipeline on a thread (injectable source, testable)
snapshot.py           IQ ring buffer + snapshot writers + background worker
dsp.py                Chunked, low-memory spectrogram + classifier reductions
classify.py           Heuristic meteor / aircraft / interference triage
reconnect.py          Survives USB read errors instead of ending the run
uptime.py             The observing-exposure log — every rate divides by this
memutil.py            Dependency-free RSS reporting
render_snapshots.py   Turn the Pi's .npz captures into PNGs (run on a PC)
session_report.py     Pings-per-hour for one session
diurnal.py            Meteor rate vs local hour, normalized by real exposure
tools/mem_soak.py     Flood the pipeline headlessly, report peak memory
deploy/pi/            Installer, systemd unit, status page, pyrtlsdr patch
deploy/INSTALL.txt    Step-by-step Pi deployment walkthrough
docs/                 The published site (GitHub Pages serves this)
```

## How it works

```
RF in  ->  RTL-SDR IQ  ->  FFT band-power  ->  EMA noise baseline
       ->  threshold + hysteresis  ->  a Ping
       ->  classify (inline)  ->  CSV row  +  snapshot (background)
```

Each detected ping is classified from its IQ window — a meteor is a brief streak
localized in the channel; an aircraft is a carrier that *sweeps* in frequency;
interference smears across the whole band — and logged to `meteor_events.csv`
with a snapshot of the moment.

## Running it on a Pi

```bash
git clone https://github.com/jrb985/radio-meteor-project.git
cd radio-meteor-project
bash deploy/pi/install.sh          # apt deps, venv, DVB blacklist, systemd unit
sudo systemctl enable --now meteor-tracker
```

Full walkthrough: [`deploy/INSTALL.txt`](deploy/INSTALL.txt). Deeper reference:
[`deploy/pi/README_PI.md`](deploy/pi/README_PI.md).

### Fitting in 1 GB

A Pi 3B+ has 1 GB shared with the OS, and the memory here is dominated by
**snapshots, not detection**: at 1.024 Msps one second of IQ is 8.2 MB, and a
queued snapshot job holds a whole event window. Naively that OOMs the Pi during
an aircraft burst — which is exactly when the queue fills.

The `pi` profile (`METEOR_PROFILE=pi`, set by the systemd unit) fixes it:

| | |
|---|---|
| `snapshot_mode="npz"` | The Pi doesn't render images. matplotlib is never imported (~90 MB of RSS) and no multi-second render runs on a slow ARM core — events are saved as compact float16 spectrograms and rendered later on a PC with `render_snapshots.py`. |
| byte-bounded queue | Bounding the snapshot queue by *job count* bounds nothing: a job is 4–25 MB. |
| capped window / rows | Bounds any single job, and shrinks the ring buffer. |
| `snapshot_skip_aircraft` | Near a busy airport these dominate the burst. |

**Measured** (`python tools/mem_soak.py --profile pi`, synthetic event flood):
**~104 MB peak RSS**, versus ~450–490 MB on the desktop defaults.

Under pressure the **snapshot** is dropped, never the **event** — every ping is
still classified and written to the CSV, so the counts (the actual science) never
degrade.

## Measuring exposure (and one trap worth knowing)

Every rate here is **counts ÷ exposure**, so exposure has to be *measured*. The
detector appends an observing log (`sessions.csv`) recording when it was actually
detecting — excluding warm-up and USB reconnect gaps — and `diurnal.py` divides by
that.

This is not bookkeeping pedantry. The obvious shortcut is to infer exposure from
the span of logged events ("the receiver was on whenever anything was logged"),
and it fails in a genuinely dangerous way:

> Observe only 22:00–06:00 for a week. That span also covers every daytime hour
> you never watched, so those hours look *observed-but-empty*. Their rate collapses
> to zero, and out comes a textbook diurnal curve — peak at ~06h, trough at ~18h —
> **manufactured entirely by your own sleep schedule.**

It looks exactly like success, which is what makes it worth engineering against.
Fed a deliberately **flat** synthetic meteor rate, the naive method invents the
curve; the logged-exposure method recovers flat and leaves unobserved hours *blank*
rather than fake-zero.

Two consequences for observing:

- **Run continuously, not just at night.** Daytime coverage is what makes the curve
  mean anything — and daytime is where the apex minimum and the radio-only daytime
  showers live.
- **Plot the control:** `python diurnal.py --class aircraft`. Aircraft follow a
  *human* daily rhythm, nothing like a meteor one. If your meteor curve tracks the
  aircraft curve, the classifier is leaking and you're plotting air traffic.

If `sessions.csv` is missing, `diurnal.py` still runs — but stamps
**"EXPOSURE GUESSED"** across the PNG, because the plot travels on its own and its
provenance has to travel with it.

## Status — read this

The detector runs, and every part of it is verified **headlessly**: randomized
ring-buffer tests, classifier output checked against the previous implementation,
a memory soak that recovers from a forced reconnect.

It has **not yet run an unattended night against real hardware.** The auto-reconnect
and low-memory work is what makes that plausible, not something that night has
already proven. Treat the first run as a shakedown. The target is the Perseids
(Aug 12–13).

Sensitivity is currently limited by a small indoor whip antenna — getting an
antenna *outside* matters far more than which antenna it is
([antenna deck](https://jrb985.github.io/radio-meteor-project/antenna.html)).

## Notes

- Classification is **heuristic**, not trained. Treat the labels as triage and
  expect to tune the thresholds in `config.py` against confirmed events.
- The Windows GUI, the diagnostics, and the deck builders live in the working
  folder but are not published here; this repo is the Pi-runnable subset plus
  the site.

## License

[MIT](LICENSE) — use it, change it, ship it. If it helps you catch a meteor,
that's the whole point.
