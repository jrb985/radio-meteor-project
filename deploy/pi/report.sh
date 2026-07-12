#!/usr/bin/env bash
# Regenerate the analysis images from meteor_events.csv. Run by hand or on a
# cron/timer, e.g. hourly:  0 * * * * /path/to/deploy/pi/report.sh
set -euo pipefail
PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJ"
# shellcheck disable=SC1091
. .venv/bin/activate
python session_report.py || true      # -> session_report.png
python diurnal.py || true             # -> diurnal.png (needs several days)
echo "Reports updated in $PROJ"
