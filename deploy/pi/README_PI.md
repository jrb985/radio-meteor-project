# Raspberry Pi 3B+ deployment

Run the Radio Meteor Tracker headless, 24/7, on a Raspberry Pi 3B+. The Python
app is already cross-platform (the Windows-only bits are guarded), so this folder
just adds the Linux deployment layer: an installer, a systemd service, a report
job, a tiny web status page, and a pyrtlsdr compatibility patch.

## What you need

**Hardware**
- Raspberry Pi 3B+ (quad A53 @1.4 GHz, 1 GB RAM, USB 2.0).
- RTL-SDR dongle + antenna (see ../../DIY_Antenna_Options.pptx).
- A GOOD 5 V / 3 A power supply. The SDR is power-hungry; undervoltage causes
  USB read errors. A powered USB hub helps if you see undervoltage warnings.
- Heatsink/fan (sustained DSP load can thermally throttle a bare 3B+).
- 16 GB+ SD card. Snapshots + logs accumulate -> consider a USB drive or
  network offload for long deployments (see "Storage").

**Software**
- Raspberry Pi OS (Lite is fine; **64-bit recommended** for faster numpy).
- Installed by install.sh: `rtl-sdr`, `librtlsdr-dev`, `python3-venv`,
  `python3-numpy`, `python3-matplotlib` (apt) + `pyrtlsdr` (pip, in a venv).
- No GUI needed. gui_app.py (Tkinter) is NOT used on the Pi; the service runs
  the CLI `meteor_detector.py`. matplotlib is still installed (the report tools
  use it headlessly) but the detector itself never imports it on the `pi`
  profile -- see "Fitting in 1 GB" below.

## Install

```bash
# copy/clone the project onto the Pi, then from the project root:
bash deploy/pi/install.sh
# edit config.py if you want different frequency/gain/thresholds, then:
sudo systemctl enable --now meteor-tracker
```

install.sh: apt installs, blacklists the DVB kernel driver (`dvb_usb_rtl28xxu`)
so it doesn't grab the dongle, creates a `--system-site-packages` venv, pip
installs pyrtlsdr, runs `patch_pyrtlsdr.py`, does a `check_device.py` smoke test,
and installs the systemd unit.

## Monitor it

```bash
journalctl -u meteor-tracker -f          # live service log
tail -f meteor.log                        # (service also appends here)
python deploy/pi/status_server.py         # web status at http://<pi-ip>:8080
bash deploy/pi/report.sh                   # regenerate session_report.png/diurnal.png
```
Cron for hourly reports:  `0 * * * * /full/path/deploy/pi/report.sh`

## Fitting in 1 GB (the `pi` profile) -- read this

The 3B+ has **1 GB shared with the OS**, and the detector's memory is dominated
not by detection but by *snapshots*: at 1.024 Msps, one second of IQ is 8.2 MB,
and a queued snapshot job holds a whole event window. On the stock desktop
settings that adds up fast enough to OOM the Pi during an aircraft burst.

So the Pi runs a **low-memory profile**, selected by `METEOR_PROFILE=pi` (the
systemd unit sets it; add `--profile pi` if you run the CLI by hand). What it
changes, biggest win first:

| Setting | Pi value | Why |
|---|---|---|
| `snapshot_mode` | `"npz"` | Don't render PNGs here. matplotlib is never imported (**~90 MB of RSS**) and the multi-second render never runs on a slow ARM core. Events are saved as compact float16 spectrograms (~1-2 MB) and rendered later on your PC. |
| `snapshot_queue_max_bytes` | 48 MB | A hard ceiling on queued IQ. Bounding the queue by *job count* bounds nothing, because a job is anywhere from ~4 MB to ~25 MB. |
| `snapshot_max_window_s` | 1.5 s | Caps any one job at ~12 MB, and shrinks the ring buffer to 2.5 s (20 MB). |
| `snapshot_skip_aircraft` | `true` | Near SeaTac these dominate. Skipping them keeps the queue and the SD card free for meteors. (They are still logged and counted.) |
| `read_block_size` | 128 Ki | Smaller USB reads, smaller transients. |

**Measured:** ~100 MB peak RSS under a synthetic event flood, vs. ~490 MB for the
desktop defaults doing the same work. Re-check any time, on the Pi or your PC:

```bash
python tools/mem_soak.py --profile pi        # floods the pipeline, prints peak RSS
```

The systemd unit also sets `MALLOC_ARENA_MAX=2` (glibc otherwise gives each
thread a 64 MB arena) and `MemoryHigh=300M` / `MemoryMax=400M` as a **backstop**:
if something regresses, systemd reclaims and restarts the service rather than
letting the kernel OOM killer pick a victim and take `sshd` with it. install.sh
sets `gpu_mem=16` in config.txt, returning ~48 MB the GPU reserves by default.

Other 3B+ notes:

- **Detection is fine.** 1.024 Msps + per-block FFT band-power is light for numpy
  on ARM; the CPU keeps up.
- **Auto-reconnect (v1.3).** A USB/read error is recovered in-process (close ->
  backoff -> reopen -> resume) via reconnect.py, so a glitch no longer ends the
  run. The systemd `Restart=always` remains the second safety net.
- **Thermal drift** is a non-issue (wideband +-90 kHz detection).
- Want images on the Pi anyway? Set `snapshot_mode="png"` -- but expect the
  queue to back up and drop renders during bursts, and watch RSS. To turn
  snapshots off entirely: `snapshot_enabled = False`.

## Getting the pictures (npz -> PNG on your PC)

The Pi captures; your PC renders.

```bash
# on your PC:
scp -r pi@<pi-ip>:~/radio_metor_tracker/snapshots  D:\pi_snaps
python render_snapshots.py --dir D:\pi_snaps      # -> a PNG beside each .npz
```

Already-rendered files are skipped, so you can re-run it after each copy;
`--force` re-renders. The PNGs are identical to what `snapshot_mode="png"` would
have produced on the Pi.

## Storage

- Snapshots: `snapshots/ping_<UTC>_<class>.npz` (~1-2 MB each on the `pi`
  profile). Far smaller and cheaper than rendering PNGs on the Pi, but a busy
  night still accumulates -- periodically rsync + prune `snapshots/`, or point
  `snapshot_dir` at a USB drive. (`snapshot_save_raw_iq` stays **off**: raw IQ is
  ~12 MB per event.)
- `meteor.log` grows; add a logrotate rule or truncate periodically.
- `meteor_events.csv` is tiny (text) -- keep it; it's the science record. Every
  event lands here **even when its snapshot is dropped or skipped**, so the
  counts are never the thing that suffers under memory pressure.

## Troubleshooting

**The Pi can't see the dongle? -> [TROUBLESHOOTING.md](TROUBLESHOOTING.md).** It is
the most common wall on a first deploy, and it has a proper diagnostic ladder
(`lsusb` -> `lsmod` -> `dmesg` -> `rtl_test` -> `vcgencmd`). Read it before
trying fixes: more than half the time this is *electrical* (power/cable/hub), not
a driver problem, and no amount of `modprobe` will help. Note the command is
`rtl_test -t`, **not** `rtl_sdr -t`.

- `rtl_test -t` -> confirms the dongle is seen and the DVB driver isn't holding
  it. If it says "usb_claim_interface error", the DVB module is still loaded:
  `sudo modprobe -r dvb_usb_rtl28xxu` **and reboot** (the blacklist only takes
  effect at boot).
- `import rtlsdr` fails with `AttributeError: function 'rtlsdr_set_dithering' not
  found` -> run `python deploy/pi/patch_pyrtlsdr.py` (install.sh already does).
- Undervoltage / random USB errors -> better PSU or powered hub;
  check `vcgencmd get_throttled` (want `0x0`).
- Permission denied on the dongle -> the `rtl-sdr` apt package installs udev
  rules; re-plug the dongle or reboot after install.

## Files here

- `install.sh` -- one-shot setup.
- `requirements.txt` -- pip deps (pyrtlsdr).
- `patch_pyrtlsdr.py` -- optional-binding compatibility patch (idempotent).
- `meteor-tracker.service` -- systemd unit (templated by install.sh); sets
  `METEOR_PROFILE=pi` and the memory backstop.
- `report.sh` -- regenerate analysis images.
- `status_server.py` -- stdlib web status page (shows free RAM + detector RSS).

In the project root, relevant here:

- `config.py` -- `PROFILES["pi"]` is the low-memory profile.
- `render_snapshots.py` -- turns the Pi's `.npz` captures into PNGs on your PC.
- `tools/mem_soak.py` -- floods the pipeline headlessly and reports peak RSS.
