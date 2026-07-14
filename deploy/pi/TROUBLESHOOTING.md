# Troubleshooting — the Pi can't see the dongle

The most common wall on a first Pi deploy. Work **down the ladder** — each rung
assumes the one above it passed. Don't skip to the driver fix; more than half the
time this is electrical, not software, and no amount of `modprobe` will help.

> **First, the command.** It is **`rtl_test -t`**, not `rtl_sdr -t`.
> `rtl_sdr` is the raw-capture tool and expects an output filename — `rtl_sdr -t`
> is not a valid invocation and will just print usage. If that's what you ran,
> try `rtl_test -t` before anything else.

---

## Run all five, then read the answers below

```bash
lsusb                                  # 1. does USB see it AT ALL?
lsmod | grep -E 'dvb|rtl|r820'         # 2. is the TV driver holding it?
sudo dmesg | tail -30                  # 3. what did the kernel say on plug-in?
which rtl_test && rtl_test -t          # 4. is the tool installed; what does it say?
vcgencmd get_throttled                 # 5. is the power clean?  (want 0x0)
```

---

## 1. `lsusb` does not list it

You are looking for a line like:

```
Bus 001 Device 004: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T
```

**If it isn't there, this is not a driver problem — the dongle never enumerated.**
Nothing downstream (blacklists, drivers, Python) can fix that. It is electrical:

- **Power.** The Pi 3B+ shares one USB controller across all four ports, and an
  RTL-SDR is a hungry device. A marginal 5 V supply is the single most common
  cause. Check rung 5 (`vcgencmd get_throttled`) — but note that a supply can be
  too weak for the dongle while still reporting `0x0`.
- **A powered USB hub fixes this more often than anything else.** If you have one,
  try it before touching software.
- **Cable / port.** Try a different port and a short, thick cable. Long thin
  extension cables drop enough voltage to prevent enumeration.
- Re-plug and immediately run `sudo dmesg | tail -30`. Silence on plug-in means
  the Pi saw nothing at all; `device descriptor read/64, error -71` (or similar)
  means it tried and failed — that's a power/cable fault, not a driver one.

## 2. `lsusb` lists it, but `lsmod` shows `dvb_usb_rtl28xxu`

The classic one. Linux ships a DVB **TV-tuner** driver that claims the dongle
first, so `rtl_test` cannot take the USB interface. Symptom is usually:

```
usb_claim_interface error -6
Failed to open rtlsdr device #0
```

`install.sh` writes the blacklist and tries to unload the module, but:

- the **blacklist only takes effect on reboot**, and
- the in-place `modprobe -r` fails silently if the module is busy (the installer
  tolerates that failure so the rest of the install can finish).

So the fix is simply to finish the job:

```bash
sudo modprobe -r dvb_usb_rtl28xxu
sudo reboot
```

After the reboot, `lsmod | grep dvb` should print **nothing**. If the module is
still there, confirm the blacklist file exists and is correct:

```bash
cat /etc/modprobe.d/blacklist-rtl.conf     # -> blacklist dvb_usb_rtl28xxu
```

If it is present and the module *still* loads, blacklist the whole family:

```bash
sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist r820t
EOF
sudo reboot
```

## 3. `which rtl_test` is empty

The apt install didn't take.

```bash
sudo apt-get update && sudo apt-get install -y rtl-sdr librtlsdr-dev
```

## 4. `rtl_test -t` says "No supported devices found"

The tool runs but sees nothing. If `lsusb` *does* list the dongle, this is a
permissions problem — the udev rules from the `rtl-sdr` package haven't been
applied to the currently-plugged device.

```bash
sudo rtl_test -t          # if this works but the un-sudo'd version doesn't,
                          # it IS permissions
```

Fix properly (don't just run everything as root — the service runs as your user):

```bash
sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules && sudo udevadm trigger
# then UNPLUG and RE-PLUG the dongle, and log out/in (or reboot)
```

## 5. `vcgencmd get_throttled` is not `0x0`

You have a power problem, and **everything above is downstream of it.** Stop
chasing drivers and fix the supply first.

| Value | Meaning |
|---|---|
| `0x0` | Healthy. |
| `0x50005` | Under-voltage now **and** since boot. |
| `0x50000` | Under-voltage happened earlier (may have been at plug-in). |

Any under-voltage bit → use a proper 5 V / 3 A supply, and preferably put the
dongle on a **powered hub** so it isn't drawing from the Pi's budget at all.

---

## Once `rtl_test -t` works

It should print something like `Found 1 device(s): 0: Realtek, RTL2838UHIDIR…`
followed by a gain list. Then confirm Python can open it too — that's a separate
layer and can fail on its own:

```bash
cd ~/radio-meteor-project
.venv/bin/python check_device.py
```

If `check_device.py` fails with a missing-function `AttributeError` (e.g.
`rtlsdr_set_dithering`), run the compatibility patch — the bundled librtlsdr build
doesn't export every optional symbol pyrtlsdr expects:

```bash
.venv/bin/python deploy/pi/patch_pyrtlsdr.py
```

## Notes

- **32-bit Raspberry Pi OS is fine.** It is not the cause of a detection failure.
  64-bit is only recommended because numpy is faster there, and the `pi` profile
  has ample CPU headroom either way.
- `install.sh` runs its device check with `|| true` so a failure there does **not**
  abort the install — meaning the install can "succeed" while the dongle was never
  visible. Always confirm with `rtl_test -t` yourself.
