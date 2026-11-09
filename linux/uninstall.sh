#!/bin/sh
# T10Link uninstaller for Linux.
#
#   sudo /opt/t10link/uninstall.sh                 remove the service, the t10link command and /opt/t10link
#   sudo /opt/t10link/uninstall.sh --keep-config   first save config, pairing, sensor numbers and saved
#                                                  setpoints to ~/t10link-backup-<date>
#   sudo /opt/t10link/uninstall.sh --yes           don't ask
#
# The thermostat keeps this bridge's HomeKit pairing. To pair again later, restore the backup
# (copy pairing.json into /opt/t10link after reinstalling), or run "t10link unpair" BEFORE
# uninstalling, or reset HomeKit on the thermostat.
set -e
DEST=/opt/t10link
UNIT=t10link-bridge
KEEP=0; YES=0
for a in "$@"; do
  case "$a" in
    --keep-config) KEEP=1 ;;
    --yes|-y) YES=1 ;;
    *) echo "unknown option: $a"; exit 1 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0 $*"; exit 1; }

echo "This will remove:"
[ -f /etc/systemd/system/$UNIT.service ] && echo "  - the $UNIT service (stops the bridge)"
[ -f /usr/local/bin/t10link ] && echo "  - /usr/local/bin/t10link"
[ -d "$DEST" ] && echo "  - $DEST$( [ "$KEEP" = 1 ] && echo ' (settings and pairing backed up first)' || echo ' (including the HomeKit pairing)')"
if [ "$YES" != 1 ]; then
  printf "Continue? [y/N] "; read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "cancelled"; exit 0 ;; esac
fi

systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
rm -f /etc/systemd/system/$UNIT.service /usr/local/bin/t10link
systemctl daemon-reload

if [ -d "$DEST" ]; then
  if [ "$KEEP" = 1 ]; then
    OWNER=$(stat -c %U "$DEST")
    HOME_DIR=$(getent passwd "$OWNER" | cut -d: -f6)
    BK="$HOME_DIR/t10link-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK"; chmod 700 "$BK"
    for f in config.json pairing.json sensors.json setpoints.json; do cp -p "$DEST/$f" "$BK/" 2>/dev/null || true; done
    chown -R "$OWNER" "$BK"
    echo "Saved settings and pairing to $BK"
  fi
  rm -rf "$DEST"
fi
echo "Uninstalled. Savant will show the bridge offline; remove the component in Blueprint if you're not reinstalling."
