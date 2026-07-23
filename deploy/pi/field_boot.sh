#!/usr/bin/env bash
# No-touch field box: at boot, observe FIELD_HOURS then power the Pi off. Invoked
# by meteor-field.service, which runs as ROOT so it can poweroff without a password.
# The DETECTOR itself is run (via runuser) as FIELD_USER, so meteor_events.csv,
# meteor.log and snapshots/ stay owned by the deploy user -- exactly as the normal
# always-on service leaves them. Not for interactive use: see field_run.sh for the
# attended version.
#
# Deliberately does NOT `set -e`: whatever happens to the detector, we must still
# reach the poweroff at the end (a field box with no operator has nowhere else to go).
set -uo pipefail

HOURS="${FIELD_HOURS:-8}"
USER_NAME="${FIELD_USER:?FIELD_USER not set (meteor-field.service provides it)}"
PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$PROJ/field_boot.log"
cd "$PROJ"

echo "$(date -u '+%F %T UTC') field boot: ${HOURS}h run as ${USER_NAME}, then poweroff" >> "$LOG"

# Let USB enumeration + udev settle after boot before opening the dongle.
sleep 10

# Run the detector as the deploy user with a hard time limit. `timeout` sends
# SIGTERM at the deadline -> the app's clean shutdown finalizes the CSV, the
# exposure log, and any queued snapshots (relies on the SIGTERM handler in
# meteor_detector.py). -k 30s SIGKILLs only if it ignores SIGTERM, which it won't.
runuser -u "$USER_NAME" -- env METEOR_PROFILE=pi \
    timeout -k 30s --signal=SIGTERM "${HOURS}h" \
    "$PROJ/.venv/bin/python" "$PROJ/meteor_detector.py" >> "$LOG" 2>&1 || true

echo "$(date -u '+%F %T UTC') detector stopped; syncing + powering off" >> "$LOG"
sync
systemctl poweroff
