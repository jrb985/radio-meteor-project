#!/usr/bin/env bash
# Render docs/status.html from the live detector state and push it to GitHub Pages.
# Run hourly from cron (as the same user that owns the clone and holds the push
# credential):   0 * * * * /home/<user>/radio-meteor-project/deploy/pi/publish_status.sh
#
# Requires a stored push credential -- a fine-grained PAT with Contents:write on
# this repo, saved via `git config credential.helper store`. See README_PI.md
# "Publishing a public status page".
set -euo pipefail

cd "$(dirname "$0")/../.."                       # -> project root

.venv/bin/python deploy/pi/publish_status.py     # writes docs/status.html

# Nothing to do if the rendered page is byte-identical to what's committed.
if git diff --quiet -- docs/status.html; then
    echo "$(date -u '+%H:%M UTC') no change"
    exit 0
fi

git add docs/status.html
git commit -q -m "status: $(date -u '+%Y-%m-%d %H:%M UTC')"

# Stay in sync with any dev commits pushed from the PC before publishing ours.
# --autostash guards the (gitignored) working tree; status.html is the only
# tracked file we touch, so a rebase here is trivial in practice.
git pull --rebase --autostash -q origin main || true
git push -q origin main
echo "$(date -u '+%H:%M UTC') published"
