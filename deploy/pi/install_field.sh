#!/usr/bin/env bash
# Install the NO-TOUCH FIELD unit: on power-on the Pi observes FIELD_HOURS (default
# 8), stops the detector cleanly, and powers itself off -- zero interaction, no SSH,
# no keyboard. Mutually exclusive with the always-on meteor-tracker service.
#
#   bash deploy/pi/install_field.sh
#
# Installs the unit but does NOT arm it (arming changes next-boot behavior to
# power-off, which could strand a remote box). The arm command is printed at the end.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
HOURS_DEFAULT=8
echo "== Installing meteor-field.service (no-touch field box) =="
echo "Project dir: $PROJ"
echo "User:        $USER"

sed "s|__PROJECT_DIR__|$PROJ|g; s|__USER__|$USER|g" deploy/pi/meteor-field.service \
    | sudo tee /etc/systemd/system/meteor-field.service >/dev/null
sudo systemctl daemon-reload
echo "Installed /etc/systemd/system/meteor-field.service"

cat <<EOF

NOT armed yet, so it won't surprise you mid-session. To ARM the field box:

  sudo systemctl disable --now meteor-tracker    # stop the always-on 24/7 service
  sudo systemctl enable meteor-field             # arms it for the NEXT boot

Then the cycle is: power on -> observe ${HOURS_DEFAULT} h -> clean shutdown ->
safe to unplug once the green activity LED goes dark. Each power-on repeats it.
Progress + the detector's output land in  $PROJ/field_boot.log  (pull the SD to
read it if the run failed).

Change the run length (e.g. 6 hours):
  sudo systemctl edit meteor-field
  # in the editor add exactly:
  #   [Service]
  #   Environment=FIELD_HOURS=6

Go back to the always-on 24/7 service:
  sudo systemctl disable meteor-field
  sudo systemctl enable --now meteor-tracker
EOF
