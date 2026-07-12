===============================================================================
 RADIO METEOR TRACKER
 RTL-SDR FM forward-scatter meteor detector  (Seattle, WA)
===============================================================================

Detects meteors by radio "forward scatter": a distant FM broadcast transmitter
that is normally below the horizon becomes briefly audible when a meteor's
ionized trail reflects its signal to the receiver. Each reflection is a "ping".

Target reference: 104.3 MHz  (locally VACANT in Seattle; carries KAWO Boise,
52 kW, ~650 km, and KSOP-FM Salt Lake City, 25 kW, ~1100 km).

-------------------------------------------------------------------------------
 HARDWARE
-------------------------------------------------------------------------------
  * RTL-SDR dongle  : RTL2832U + Rafael Micro R820T tuner  (USB 0BDA:2838)
  * Antenna         : small whip (adequate for testing; a horizontal dipole or
                      a 3-element Yagi aimed ~SE toward Boise is a big upgrade)
  * Host            : Windows 11, Python 3.14 (64-bit)

-------------------------------------------------------------------------------
 SOFTWARE / DEPENDENCIES
-------------------------------------------------------------------------------
  * pyrtlsdr 0.4.0  (patched -- see NOTE below)
  * numpy, matplotlib, python-pptx
  * librtlsdr DLLs  : bundled under tools/rtl-sdr/  (rtl-sdr-blog v1.3.6)
  * WinUSB driver   : bound to the dongle's Interface 0 via tools/rtl-sdr/zadig.exe

  NOTE - pyrtlsdr patch: the bundled rtl-sdr-blog DLL does not export a few
  optional functions (rtlsdr_set_dithering, GPIO helpers). We patched
  site-packages/rtlsdr/librtlsdr.py to bind them optionally (no-op stubs) so
  import + RtlSdr() work. A fresh/reinstalled pyrtlsdr will revert this and
  must be re-patched.

-------------------------------------------------------------------------------
 FILES
-------------------------------------------------------------------------------
  rtl_sdr_env.py        Registers the bundled librtlsdr DLL dir (import first).
  config.py             All tunable parameters (frequency, detection, snapshots).
  check_device.py       Opens the dongle, captures IQ, prints tuner/stats.
  fm_scan.py            Sweeps FM band, lists strongest carriers (reception test).
  fm_vacancy.py         Ranks FM channels by occupancy -> finds VACANT channels.
  spectrograph.py       One-shot spectrogram PNG of a chosen frequency.
  live_spectrograph.py  Real-time scrolling waterfall window (TkAgg GUI).
  monitor.py            Timed headless capture -> power-vs-time + waterfall PNGs.
  meteor_detector.py    The detector: band-power -> baseline -> ping -> CSV (+snapshots).
  snapshot.py           v1.1 IQ ring buffer (v1.4: circular, preallocated) +
                        spectrogram/npz writers + v1.3 async SnapshotWorker
                        (background writes, v1.4 byte-bounded queue).
  dsp.py                v1.4 chunked float32 spectrogram + band_stats: keeps the
                        per-event FFT transient at a few MB instead of ~200 MB.
  memutil.py            v1.4 dependency-free RSS reporting (heartbeat, soak test).
  render_snapshots.py   v1.4 render the Pi's .npz captures to PNGs on your PC.
  tools/mem_soak.py     v1.4 headless memory soak: floods the pipeline, reports
                        peak RSS. Run with --profile pi to check the 1 GB budget.
  reconnect.py          v1.3 ReconnectingReader: auto-recover from USB/read errors.
  classify.py           v1.2 heuristic meteor/aircraft/interference classifier.
  capture_engine.py     v1.2 threaded capture engine (SDR -> queue of updates).
  gui_app.py            v1.3 Tkinter GUI: Live tab (waterfall + side gallery) +
                        Gallery tab (big image viewer with Prev/Next).
  session_report.py     v1.2 pings-per-hour chart + summary from the CSV.
  diurnal.py            Science tool: meteor rate vs local hour-of-day (diurnal
                        curve), meteor-only, exposure-normalized w/ error bars.
  build_docs_pptx.py    Regenerates Radio_Meteor_Tracker.pptx.
  tools/rtl-sdr/        librtlsdr DLLs, CLI tools, and zadig.exe.

  Outputs:
  meteor_events.csv     One row per detected ping (start_utc, duration, SNR, power).
  snapshots/            v1.1: one PNG per ping (raw IQ .npy optional).
  monitor_*.png         Diagnostic plots from monitor.py.

-------------------------------------------------------------------------------
 HOW IT WORKS (detection pipeline)
-------------------------------------------------------------------------------
  1. Tune 250 kHz below 104.3 so the whole ~180 kHz FM channel sits clear of
     the RTL-SDR DC spike, at +250 kHz baseband.
  2. Read IQ continuously (1.024 Msps). FFT each ~4 ms block; sum the power in
     the +-90 kHz detection band = "channel power".
  3. Track a slow EMA "noise floor" baseline while the channel is quiet.
  4. Ping = channel power rises >= snr_threshold_db above baseline; ends when it
     drops below (threshold - hysteresis). Accept only min_ping_ms..max_ping_ms.
  5. Log to CSV; (v1.1) save a high-res spectrogram snapshot of the moment.

-------------------------------------------------------------------------------
 RUNNING
-------------------------------------------------------------------------------
  python check_device.py            # confirm the dongle works
  python fm_vacancy.py              # (re)confirm 104.3 is locally clear
  python live_spectrograph.py 104.3 # watch the channel live
  python monitor.py 600             # 10-minute diagnostic capture
  python meteor_detector.py         # run the detector, CLI (Ctrl+C to stop)
  python gui_app.py                 # v1.2 GUI (detector + gallery + report)
  python session_report.py          # summarize meteor_events.csv for a session
  python diurnal.py                 # meteor rate vs local hour (needs several days)

===============================================================================
 VERSION HISTORY
===============================================================================

-------------------------------------------------------------------------------
 v1.0  --  Baseline detector (log + count)
-------------------------------------------------------------------------------
  * BandPowerAnalyzer: FFT band-power around the target channel.
  * EventDetector: EMA noise floor + threshold/hysteresis event detection.
  * CsvSink: appends one row per ping to meteor_events.csv.
  * Tuning hardening discovered during bring-up and folded into v1.0 config:
      - FIXED gain (40.2 dB), NOT "auto": AGC hunting produced band-wide power
        swings that masqueraded as pings and made the baseline wander.
      - warmup_skip_s (20 s): ignore tuner/baseline settling at startup.
      - max_ping_ms (2000): reject multi-second events (aircraft/tropo scatter).
      - snr_threshold_db lowered 6 -> 4 dB (baseline is very stable, ~0.7 dB).

-------------------------------------------------------------------------------
 v1.1  --  Trigger snapshots  (THIS RELEASE)
-------------------------------------------------------------------------------
  WHY: a plain ping count cannot tell a meteor from an aircraft reflection or
  interference. We need to SEE each event at high resolution.

  NEW - snapshot.py:
    * IQRingBuffer: fixed-capacity rolling buffer of the most recent IQ,
      tracking absolute sample indices so any [start, stop) window can be
      recovered after a ping fires.
    * save_snapshot(): renders a zoomed spectrogram PNG per ping covering
      pre-roll + event + post-roll, with the target channel and detection band
      marked, time measured relative to ping start. Optional raw-IQ (.npy) dump.

  CHANGED - meteor_detector.py:
    * Ping gained start_sample / end_sample (absolute IQ indices).
    * EventDetector.update() takes sample_pos and records event sample range.
    * main() maintains the ring buffer, passes sample positions, and saves a
      snapshot for every logged ping. Snapshot errors are caught so they can
      never interrupt capture.

  CHANGED - config.py:
    * snapshot_enabled, snapshot_dir, snapshot_pre_roll_s, snapshot_post_roll_s,
      snapshot_nfft, snapshot_save_raw_iq.

  HOW TO READ A SNAPSHOT:
    * Meteor       -> brief bright streak LOCALIZED at 104.3 (+ optional short
                      Doppler tail).
    * Aircraft     -> a carrier line that SWEEPS in frequency over seconds.
    * Interference -> broadband smear across the whole window.

-------------------------------------------------------------------------------
 v1.2  --  GUI + classification + reporting  (IN PROGRESS)
-------------------------------------------------------------------------------
  DONE:
    * capture_engine.py: runs the v1.1 pipeline on a background thread and
      publishes spectrum/ping/status messages to a thread-safe queue. Accepts an
      injectable sample source for headless testing (no hardware needed).
    * gui_app.py: Tkinter window (matplotlib waterfall via TkAgg) with live
      spectrum + scrolling waterfall, Start/Stop, running ping counter, per-class
      tally, event log, a scrollable SNAPSHOT GALLERY (class-colored thumbnails,
      click to enlarge), a Report button, and LIVE controls for target frequency,
      gain, and threshold.   Run:  python gui_app.py
    * classify.py: heuristic per-ping classifier ->
        meteor       = brief, energy concentrated in the channel, low drift
        aircraft     = in-channel but frequency DRIFTS (Doppler) / longer event
        interference = energy spread across the whole span (broadband)
      Thresholds live in config.py (classify_*). Classification is written to the
      snapshot filename/title, the CSV, and the GUI.
    * session_report.py: pings-per-hour chart stacked by class + text summary
      (meteors/hr, span, per-class counts).   Run:  python session_report.py
    * CSV schema gained a 'classification' column; an older-format
      meteor_events.csv is auto-rotated to meteor_events.csv.old on first write.
    * EventDetector.snr_threshold_db is live-adjustable from the GUI.
    * Verified headlessly: capture engine (synthetic source -> ping, classify,
      snapshot, CSV), classifier (meteor/aircraft/interference synthetics),
      session report; all modules byte-compile and import. NOTE: the GUI window
      must be launched on a desktop session (Tk needs an interactive window
      station), so it was verified by construction/compile, not by driving it.
  TODO:
    * Trained (vs heuristic) classification; confidence tuning on real events.

-------------------------------------------------------------------------------
 v1.3  --  Tabbed UI: Live + Gallery  (IN PROGRESS)
-------------------------------------------------------------------------------
  gui_app.py reorganized into a ttk.Notebook with two tabs (shared control bar
  and capture engine across both):
    * Live tab    : the v1.2 view -- waterfall + spectrum, controls, counter,
                    per-class tally, event log, and the scrollable thumbnail
                    gallery down the side.
    * Gallery tab : a big single-image viewer for reviewing a snapshot at
                    (near) full size, with Prev/Next buttons and a full event
                    list to pick from.
  Toggle: clicking a thumbnail on the Live tab jumps to the Gallery tab and
  shows that event enlarged. The big view follows the newest event unless you
  have navigated back to an older one.
  Verified: byte-compiles and imports; new methods present. (GUI window still
  needs a desktop session to run -- launch: python gui_app.py)

  Robustness upgrades (BUILT, shared by CLI + GUI + Pi):
    * Auto-reconnect (reconnect.py -> ReconnectingReader): a USB/read error no
      longer ends a run -- it closes, backs off (exponential, capped), reopens,
      and resumes. Config: reconnect_enabled / _backoff_start_s / _backoff_max_s
      / _give_up_after (0 = never). The CLI prints a heartbeat every 5 min.
    * Async snapshots (snapshot.py -> SnapshotWorker): matplotlib rendering runs
      on a background thread via a bounded queue, so it never stalls the capture
      read. If the worker falls behind during a burst, extra RENDERS are dropped
      (events are still logged/classified) -- a built-in governor that also
      bounds CPU/disk. save_snapshot now uses the OO Figure API (thread-safe).
      Config: snapshot_async / snapshot_queue_max / snapshot_skip_aircraft.
    * Verified headlessly: engine recovers from a simulated read error and keeps
      streaming; worker renders off-thread; submit is non-blocking; drops occur
      under load without stalling the reader.
  TODO: dark theme, MHz axis on first frame, packaging to an .exe; trained
  classifier.

-------------------------------------------------------------------------------
 v1.4  --  Low-memory profile: fitting the 1 GB Raspberry Pi 3B+  (BUILT)
-------------------------------------------------------------------------------
  The problem: memory is dominated by SNAPSHOTS, not detection. At 1.024 Msps
  one second of IQ is 8.2 MB, and a queued snapshot job holds a whole event
  window. Under the v1.3 defaults a burst of events (aircraft, near SeaTac)
  could put ~790 MB of IQ in the queue -- an OOM on a 1 GB Pi. Measured under a
  synthetic event flood: ~490 MB peak on the desktop defaults, ~100 MB on the
  new "pi" profile.

  Four fixes, all shared by CLI + GUI + Pi:
    * dsp.py (NEW): spectrograms are computed in ROW BLOCKS at float32 instead
      of one big float64 FFT. numpy's FFT always upcasts to complex128, so the
      old one-shot approach spiked ~150-200 MB per event; now it is a few MB
      regardless of event length. classify.py reduces to per-row scalars
      (dsp.band_stats) and never builds the full spectrogram at all. Verified
      to produce identical labels and features to the v1.3 math.
    * snapshot.py IQRingBuffer is now a PREALLOCATED circular buffer. It used to
      np.concatenate a fresh array every read -- two ~33 MB allocations 4x/sec,
      ~260 MB/s of memcpy and a fragmented heap after a 12 h run. Also gained
      clear() (called after a reconnect, when sample-index continuity breaks).
    * SnapshotWorker's queue is bounded by BYTES (snapshot_queue_max_bytes), not
      job count -- a job is anywhere from ~4 MB to ~25 MB, so a count bounded
      nothing. Over the cap, the snapshot is dropped; the EVENT is still
      classified and logged to the CSV. Also: snapshot_max_window_s caps any one
      job, snapshot_max_rows max-pools the spectrogram (a ~1000 px figure cannot
      resolve 3000 rows; max, not mean, preserves the ping).
    * snapshot_mode = "png" | "npz" | "off". matplotlib is now imported LAZILY,
      inside the PNG renderer only, so "npz" never loads it (~90 MB of RSS) and
      never runs a multi-second render on a slow ARM core. "npz" saves a
      decimated float16 spectrogram (~1-2 MB); render_snapshots.py (NEW) turns
      those into identical PNGs later on a PC.

  config.py PROFILES["pi"] bundles the above; select it with METEOR_PROFILE=pi
  (the systemd unit does) or `python meteor_detector.py --profile pi`. The Pi
  service also sets MALLOC_ARENA_MAX=2 and a MemoryHigh/MemoryMax backstop, and
  install.sh sets gpu_mem=16.

  Verified headlessly: 36k randomized ring-buffer probes match a naive oracle;
  classifications unchanged vs v1.3 on meteor/aircraft/interference cases;
  npz -> PNG round trip renders correctly; the soak test recovers from a forced
  reconnect. Real-hardware run on a Pi still pending.
  New: dsp.py, memutil.py, render_snapshots.py, tools/mem_soak.py.
    python tools/mem_soak.py --profile pi     # floods the pipeline, prints RSS
    python render_snapshots.py --dir <dir>    # .npz -> .png on your PC

===============================================================================
 KNOWN CAVEATS
===============================================================================
  * Small whip antenna limits sensitivity; expect low ping rates.
  * Summer sporadic-E (Jun-Aug) can bring distant stations in directly for
    minutes -> false pings; real meteor pings are brief (sub-second).
  * Only one process may hold the dongle at a time.
  * The pyrtlsdr patch lives in site-packages and is lost on reinstall.
===============================================================================
