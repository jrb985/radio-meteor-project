#!/usr/bin/env bash
# Set up the Radio Meteor Tracker on a Raspberry Pi (Raspberry Pi OS, 64-bit
# recommended). Run from anywhere: bash deploy/pi/install.sh
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
echo "== Radio Meteor Tracker -- Pi install =="
echo "Project dir: $PROJ"
echo "User:        $USER"

echo "== 1/6 apt packages =="
sudo apt-get update
sudo apt-get install -y rtl-sdr librtlsdr-dev \
    python3 python3-venv python3-pip python3-numpy python3-matplotlib

echo "== 2/6 blacklist the DVB kernel driver (frees the dongle) =="
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

echo "== 3/6 python venv (+ system numpy/matplotlib) =="
cd "$PROJ"
python3 -m venv --system-site-packages .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
pip install -r deploy/pi/requirements.txt

echo "== 4/6 make pyrtlsdr tolerant of the librtlsdr build =="
python deploy/pi/patch_pyrtlsdr.py || true

echo "== 5/6 quick device check =="
if command -v rtl_test >/dev/null; then
    echo "Running 'rtl_test -t' (Ctrl+C ok)..."; timeout 4 rtl_test -t || true
fi
python check_device.py || echo "(check_device.py failed -- see troubleshooting in README_PI.md)"

echo "== 6/7 free RAM the GPU is holding =="
# A headless Pi needs no framebuffer. The default split reserves 64 MB of the
# 1 GB for the GPU; 16 MB is the minimum and hands ~48 MB back to the detector.
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
if [ -f "$BOOTCFG" ]; then
    if grep -q '^gpu_mem=' "$BOOTCFG"; then
        echo "  gpu_mem already set: $(grep '^gpu_mem=' "$BOOTCFG")"
    else
        echo 'gpu_mem=16' | sudo tee -a "$BOOTCFG" >/dev/null
        echo "  gpu_mem=16 appended to $BOOTCFG (takes effect on reboot)"
    fi
else
    echo "  (no config.txt found -- skipping; set gpu_mem=16 by hand)"
fi

echo "== 7/7 install systemd service =="
sed "s|__PROJECT_DIR__|$PROJ|g; s|__USER__|$USER|g" deploy/pi/meteor-tracker.service \
    | sudo tee /etc/systemd/system/meteor-tracker.service >/dev/null
sudo systemctl daemon-reload

cat <<EOF

Done. Next:
  sudo systemctl enable --now meteor-tracker      # start on boot + now
  journalctl -u meteor-tracker -f                 # or: tail -f $PROJ/meteor.log
  python deploy/pi/status_server.py               # web status on :8080
Edit config.py first if you want to change frequency/gain/thresholds.

MEMORY: the service runs with METEOR_PROFILE=pi, which fits the 1 GB 3B+
(snapshot_mode="npz" -- events are saved as compact .npz, NOT rendered here).
Turn them into PNGs on your PC:
  scp -r $USER@\$(hostname):$PROJ/snapshots  D:\\pi_snaps
  python render_snapshots.py --dir D:\\pi_snaps
Check the budget any time with:  python tools/mem_soak.py --profile pi
EOF
