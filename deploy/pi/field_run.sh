#!/usr/bin/env bash
# Field run: observe for a fixed number of hours, stop the detector CLEANLY, then
# power the Pi off so it is SAFE TO UNPLUG. Pulling power on a running Pi risks SD
# corruption and skips the exposure-log finalize; this avoids both.
#
#   bash deploy/pi/field_run.sh [HOURS]        # default 8
#
# You enter your sudo password ONCE up front (to schedule the poweroff); the run
# itself is unattended. Cancel a still-pending poweroff any time:  sudo shutdown -c
set -euo pipefail

HOURS="${1:-8}"
if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || [ "$HOURS" -lt 1 ]; then
    echo "usage: field_run.sh [HOURS>=1]  (got '$HOURS')" >&2
    exit 2
fi
MINS=$(( HOURS * 60 ))
cd "$(dirname "$0")/../.."

# Don't fight the always-on service for the dongle, if it happens to be running.
if systemctl is-active --quiet meteor-tracker 2>/dev/null; then
    echo "Stopping the always-on meteor-tracker service for this field run..."
    sudo systemctl stop meteor-tracker
fi

# Schedule the OS poweroff up front. `shutdown -h +N` returns immediately and does
# a proper shutdown at the deadline (syncs + unmounts the SD before halting).
sudo shutdown -h "+${MINS}"
echo "Poweroff scheduled in ${HOURS}h. Detector starting; it stops cleanly ~1 min early."

# Run the detector until ~1 min before poweroff. `timeout` sends SIGTERM at the
# deadline -> the in-app clean shutdown finalizes the CSV + exposure log + snapshots
# (-k 30s SIGKILLs only if it somehow ignores that, which it won't).
STOP_AFTER=$(( MINS - 1 ))
METEOR_PROFILE=pi timeout -k 30s --signal=SIGTERM "${STOP_AFTER}m" \
    .venv/bin/python meteor_detector.py || true

echo
echo "Detector stopped cleanly. The Pi will power off at the scheduled time."
echo "Safe to unplug once the green activity LED goes dark.  (Cancel: sudo shutdown -c)"
