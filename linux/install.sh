#!/bin/sh
# T10Link installer for Linux: Raspberry Pi OS (32- or 64-bit), Debian 12 / 13, Ubuntu 22.04+.
#
#   sh linux/install.sh                 install or upgrade; the service runs as you
#   sh linux/install.sh --user NAME     run the service as another (existing) user
#
# Needs sudo, Python 3.10+ and internet access once (to download aiohomekit). Re-running upgrades
# in place and keeps config.json, the HomeKit pairing, sensor numbering and saved setpoints.
set -e
cd "$(dirname "$0")/.."
DEST=/opt/t10link
UNIT=t10link-bridge
AIOHOMEKIT=aiohomekit==4.0.1
USER_NAME=${T10LINK_USER:-$(id -un)}
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Linux" ] || fail "this installer is for Linux (on a Mac use: sudo ./macos/install.sh)"
command -v systemctl >/dev/null || fail "systemd is required"
id "$USER_NAME" >/dev/null 2>&1 || fail "user '$USER_NAME' does not exist"
[ "$USER_NAME" != "root" ] || fail "don't run the service as root: run this as a normal user (it uses sudo), or add --user <name>"

# ---- Python 3.10+ with venv ------------------------------------------------
say "Checking Python"
PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer is needed (found: $(python3 --version 2>&1)). Raspberry Pi OS Bookworm or newer has it."
if ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then
  if command -v apt-get >/dev/null; then
    PKG="python$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')-venv"
    say "Installing $PKG (Python's venv module)"
    sudo apt-get update -qq && { sudo apt-get install -y -qq "$PKG" || sudo apt-get install -y -qq python3-venv; }
  fi
  "$PY" -c 'import ensurepip, venv' 2>/dev/null || fail "Python's venv module is missing: sudo apt install python3-venv"
fi
echo "    $("$PY" --version) at $PY, $(uname -m)"

# ---- Port --------------------------------------------------------------------
CFG="bridge/config.json"; [ -f "$DEST/config.json" ] && CFG="$DEST/config.json"
PORT=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('tcp_port', 2400))" "$CFG")
if command -v ss >/dev/null && ss -Hltn "sport = :$PORT" | grep -q .; then
  if ! systemctl is-active --quiet "$UNIT"; then
    ss -ltnp "sport = :$PORT" 2>/dev/null || true
    fail "port $PORT is already in use. Change tcp_port in $CFG (and the port in Blueprint), then rerun."
  fi
fi

# ---- Files -------------------------------------------------------------------
NEW_VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' bridge/t10link_bridge.py)
OLD_VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/t10link_bridge.py" 2>/dev/null || true)
say "Installing T10Link $NEW_VER to $DEST${OLD_VER:+ (replacing $OLD_VER)}, service user $USER_NAME"
sudo mkdir -p "$DEST"
sudo chown "$USER_NAME" "$DEST"
sudo chmod 700 "$DEST"          # pairing.json (HomeKit keys) lives here
sudo install -m 644 -o "$USER_NAME" bridge/t10link_bridge.py bridge/hap_local.py bridge/t10link_web.py "$DEST/"
if [ -f "$DEST/config.json" ]; then
  echo "    keeping existing config.json"
else
  sudo install -m 644 -o "$USER_NAME" bridge/config.json "$DEST/config.json"
  echo "    new config.json: $DEST/config.json"
fi

# ---- Python environment ------------------------------------------------------
as_user() { sudo -u "$USER_NAME" -H "$@"; }
PYVER='import sys; print("%d.%d" % sys.version_info[:2])'
if [ -x "$DEST/venv/bin/python" ] && [ "$("$DEST/venv/bin/python" -c "$PYVER" 2>/dev/null)" = "$("$PY" -c "$PYVER")" ]; then
  echo "    reusing $DEST/venv"
else
  say "Creating Python environment $DEST/venv"
  sudo rm -rf "$DEST/venv"
  as_user "$PY" -m venv "$DEST/venv"
fi
if ! as_user "$DEST/venv/bin/python" -I -c "import aiohomekit; from importlib.metadata import version; assert version('aiohomekit') == '${AIOHOMEKIT#*==}'" 2>/dev/null; then
  say "Installing $AIOHOMEKIT (needs internet; a few minutes on a Pi)"
  set -- --only-binary=:all: --no-binary=chacha20poly1305
  case "$(uname -m)" in
    armv6l|armv7l) set -- "$@" --extra-index-url https://www.piwheels.org/simple ;;  # 32-bit Pi OS
  esac
  # chacha20poly1305 0.0.3 is pure Python and only published as source; everything else uses
  # prebuilt wheels (PyPI, or piwheels on 32-bit ARM), so no compiler is needed.
  as_user "$DEST/venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
  as_user "$DEST/venv/bin/python" -m pip install -q --disable-pip-version-check "$@" "$AIOHOMEKIT" \
    || fail "installing $AIOHOMEKIT failed - see the messages above"
fi
as_user "$DEST/venv/bin/python" -I -c "from aiohomekit.controller.ip.controller import IpController" \
  || fail "aiohomekit doesn't import - see the messages above"
as_user sh -c "'$DEST/venv/bin/pip' freeze --disable-pip-version-check > '$DEST/requirements.lock'"

# ---- Service and command -----------------------------------------------------
sed "s/^User=.*/User=$USER_NAME/" linux/t10link-bridge.service | sudo tee /etc/systemd/system/$UNIT.service >/dev/null
sed "s/^RUN_AS=.*/RUN_AS=$USER_NAME/" linux/t10link | sudo tee /usr/local/bin/t10link >/dev/null
sudo chmod 755 /usr/local/bin/t10link
sudo cp linux/uninstall.sh "$DEST/uninstall.sh"
sudo chmod 755 "$DEST/uninstall.sh"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT" >/dev/null 2>&1
sudo systemctl restart "$UNIT"
echo "$(date '+%Y-%m-%d %H:%M:%S') $NEW_VER" | as_user tee -a "$DEST/install.log" >/dev/null

ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || { journalctl -u "$UNIT" -n 20 --no-pager; fail "the service did not start - see the log above"; }

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Installed and running"
cat <<EOF

    Bridge version:  $NEW_VER
    Savant profile:  Device IP Address ${IP:-<this machine's IP>}, port $PORT
    Config:          $DEST/config.json   (then: t10link restart)
    Web page:        http://${IP:-<this machine's IP>}:$(sed -n 's/.*"web_port": *\([0-9]*\).*/\1/p' "$DEST/config.json" | head -1 | grep . || echo 2401)   (status, pairing, adding thermostats)
    Control:         t10link status | states | logs | restart | pair | dump
    Remove:          sudo $DEST/uninstall.sh

EOF
NEXT=1
grep -q '"192.168.1.50"' "$DEST/config.json" && { echo "  $NEXT. Edit $DEST/config.json: each thermostat's ip under "thermostats", and add the Savant host to allowed_clients."; NEXT=$((NEXT+1)); }
[ -f "$DEST/pairing.json" ] || echo "  $NEXT. t10link pair   (on the thermostat: Menu > Connect HomeKit shows the code; repeat for each thermostat)"
