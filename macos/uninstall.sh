#!/bin/bash
# zsh is the macOS default shell: if started with "zsh file" or "sh file", relaunch in bash
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
# T10Link uninstaller for macOS
#
#   sudo ./uninstall.sh                 remove everything install.sh added (asks to confirm)
#   sudo ./uninstall.sh --keep-config   first save config, pairing, sensor numbers and saved
#                                       setpoints to ~/T10Link-backup-<date> (install.sh restores it)
#   sudo ./uninstall.sh --yes           don't ask
#   sudo ./uninstall.sh --dir /path     install folder, if not ~/T10Link
#
# Reverses only what install.sh recorded in .install-manifest: the launchd service,
# /usr/local/bin/t10link, the firewall exception (only if install added it), /usr/local/bin (only
# if install created it) and the install folder (including its Python environment).
# The thermostat keeps this bridge's HomeKit pairing: keep the backup, or run "t10link unpair" first.
set -uo pipefail

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

KEEP=0; YES=0; DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-config) KEEP=1; shift ;;
    --yes|-y)      YES=1; shift ;;
    --dir)         DIR="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

[ "$(uname)" = "Darwin" ] || fail "this uninstaller is for macOS"
[ "$(id -u)" -eq 0 ] || fail "run with sudo: sudo $0"

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$DIR" ]; then
  if [ -f "$HERE/.install-manifest" ]; then
    DIR="$HERE"
  else
    U="${SUDO_USER:-${USER:-$(id -un)}}"
    DIR="$(dscl . -read "/Users/$U" NFSHomeDirectory 2>/dev/null | awk '{print $2}')/T10Link"
  fi
fi

LABEL="org.t10link.bridge"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
DEST="$DIR"; RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"; PYTHON=""; FW_ADDED=0; CREATED_BIN=0
if [ -f "$DIR/.install-manifest" ]; then
  # bash 3.2 (macOS /bin/bash) can't source <(...), so eval the known keys
  eval "$(grep -E '^(LABEL|PLIST|DEST|RUN_USER|PYTHON|FW_ADDED|CREATED_BIN)=[^;&|$\`]*$' "$DIR/.install-manifest")"
else
  warn "no install record in $DIR - removing the service and folder only; any firewall entry is left alone"
fi

# safety: only ever delete a folder named T10Link
case "$DEST" in
  */T10Link) ;;
  *) fail "refusing to delete '$DEST' (expected a folder named T10Link). Use --dir." ;;
esac

echo
echo "This will remove:"
[ -f "$PLIST" ] && echo "  - launchd service $LABEL (stops the bridge)"
[ -L /usr/local/bin/t10link ] && echo "  - /usr/local/bin/t10link"
[ "$FW_ADDED" = "1" ] && echo "  - firewall exception for $PYTHON"
[ -d "$DEST" ] && echo "  - $DEST$( [ "$KEEP" = 1 ] && echo ' (settings and pairing backed up first)' || echo ' (including the HomeKit pairing)')"
echo
if [ "$YES" -ne 1 ]; then
  read -r -p "Continue? [y/N] " a
  case "$a" in y|Y|yes|YES) ;; *) echo "cancelled"; exit 0 ;; esac
fi

if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  say "Stopping the bridge"
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 20); do pgrep -f "$DEST/t10link_bridge.py" >/dev/null || break; sleep 1; done
  pgrep -f "$DEST/t10link_bridge.py" >/dev/null && { warn "still running - killing"; pkill -9 -f "$DEST/t10link_bridge.py"; }
fi
launchctl enable "system/$LABEL" 2>/dev/null || true
[ -f "$PLIST" ] && { say "Removing $PLIST"; rm -f "$PLIST"; }

if [ -L /usr/local/bin/t10link ]; then
  say "Removing /usr/local/bin/t10link"
  rm -f /usr/local/bin/t10link
fi
if [ "$CREATED_BIN" = "1" ] && [ -d /usr/local/bin ] && [ -z "$(ls -A /usr/local/bin)" ]; then
  rmdir /usr/local/bin
fi

FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if [ "$FW_ADDED" = "1" ] && [ -n "$PYTHON" ] && [ -x "$FW" ]; then
  # leave it if another installed bridge (HaloKey, OpenWx, ...) may rely on the same Python exception
  if [ -f /Library/LaunchDaemons/org.halolink.bridge.plist ] || [ -f /Library/LaunchDaemons/org.openwx.bridge.plist ]; then
    warn "leaving the Python firewall exception in place: another bridge on this Mac may use it"
  else
    say "Removing firewall exception for Python"
    "$FW" --remove "$PYTHON" >/dev/null 2>&1 || warn "could not remove the firewall entry; remove Python in Firewall Options manually"
  fi
fi

if [ -d "$DEST" ]; then
  if [ "$KEEP" = "1" ]; then
    BK="$(dirname "$DEST")/T10Link-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK"; chmod 700 "$BK"
    for f in config.json pairing.json sensors.json setpoints.json; do cp -p "$DEST/$f" "$BK/" 2>/dev/null || true; done
    chown -R "$RUN_USER" "$BK"
    say "Saved settings and pairing to $BK"
  fi
  say "Removing $DEST"
  rm -rf "$DEST"
fi

say "Uninstalled. Savant will show the bridge offline (SYS_ONLINE = 0);"
echo "    remove the component in Blueprint if you're not reinstalling."
