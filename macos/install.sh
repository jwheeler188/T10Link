#!/bin/bash
# zsh is the macOS default shell: if started with "zsh file" or "sh file", relaunch in bash
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
# T10Link installer for macOS (a Savant Pro host Mac, or any Mac that's always on).
#
#   sudo ./install.sh                 install or upgrade; the service runs as you
#   sudo ./install.sh --user RPM      run the service as a specific macOS user
#   --install-python                  install Python from python.org without asking, if it's needed
#   --no-open                         don't open the web page at the end
#
# Needs Python 3.10 or newer. Apple's /usr/bin/python3 is 3.9, so if there's no newer one the
# installer offers to install Python 3.13 from python.org: the framework only, with no "python3"
# command, PATH or shell profile changes, so other apps (Savant's own scripts on a Savant host)
# keep the python3 they use now. Needs internet access once, to download aiohomekit into a private
# environment in ~/T10Link/venv. Upgrades keep config.json, the HomeKit pairing, sensor numbering
# and saved setpoints.
set -euo pipefail
trap 'printf "\033[31mERROR:\033[0m install stopped at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

LABEL="org.t10link.bridge"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
AIOHOMEKIT="aiohomekit==4.0.1"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../bridge" && pwd)"

# python.org installer used when no Python 3.10+ is found (checked by SHA-256 and signature)
PY_PKG_VER="3.13.17"
PY_PKG_URL="https://www.python.org/ftp/python/${PY_PKG_VER}/python-${PY_PKG_VER}-macos11.pkg"
PY_PKG_SHA256="3053592af66128269efab37fe7851a4894920b27c442aeec99ce4d34d17c07e3"

RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"
AUTO_PY=0; OPEN_PAGE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --install-python) AUTO_PY=1; shift ;;
    --no-open) OPEN_PAGE=0; shift ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || fail "this installer is for macOS (on Linux use: sh linux/install.sh)"
[ "$(id -u)" -eq 0 ] || fail "run with sudo: sudo $0"
id "$RUN_USER" >/dev/null 2>&1 || fail "user '$RUN_USER' does not exist"
[ "$RUN_USER" != "root" ] || fail "refusing to run the service as root; use --user <name>"

USER_HOME="$(dscl . -read "/Users/$RUN_USER" NFSHomeDirectory | awk '{print $2}')"
DEST="$USER_HOME/T10Link"

# On Apple Silicon, run Python natively even if this Terminal runs under Rosetta: the packages pip
# installs must match the architecture launchd starts the bridge with.
ARCH=()
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then ARCH=(/usr/bin/arch -arm64); fi
# ${ARCH[@]+...}: bash 3.2 with set -u treats an empty array as unbound
as_user() { sudo -u "$RUN_USER" -H ${ARCH[@]+"${ARCH[@]}"} "$@"; }

# ---- Python 3.10+ ------------------------------------------------------------
# yes/no question on the terminal (works under "curl ... | bash" too); no terminal = no
ask() {
  local a=""
  [ -r /dev/tty ] || return 1
  printf '%s [Y/n] ' "$1" > /dev/tty
  read -r a < /dev/tty || return 1
  case "$a" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

find_python() {
  PY=""
  # newest python.org install first, then Homebrew / other installs, then Apple's
  local c CANDIDATES
  CANDIDATES="$(ls -1d /Library/Frameworks/Python.framework/Versions/3.* 2>/dev/null | sort -t. -k2 -n -r | sed 's|$|/bin/python3|')"
  CANDIDATES="$CANDIDATES /opt/homebrew/bin/python3 /usr/local/bin/python3"
  # /usr/bin/python3 is a stub that pops up an install dialog unless the Command Line Tools exist
  xcode-select -p >/dev/null 2>&1 && CANDIDATES="$CANDIDATES /usr/bin/python3"
  for c in $CANDIDATES; do
    if [ -x "$c" ] && as_user "$c" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$c"; return 0
    fi
  done
  return 1
}

# python.org's installer, framework only: no /usr/local/bin/python3 links, no PATH or shell
# profile changes, no IDLE / Python Launcher apps. Nothing that runs "python3" changes.
install_python() {
  local tmp mm c
  tmp="$(mktemp -d /tmp/t10link-python.XXXXXX)"
  say "Downloading Python $PY_PKG_VER from python.org"
  curl -fL --retry 3 --progress-bar -o "$tmp/python.pkg" "$PY_PKG_URL" || fail "download failed: $PY_PKG_URL"
  echo "$PY_PKG_SHA256  $tmp/python.pkg" | shasum -a 256 -c - >/dev/null 2>&1 \
    || fail "the downloaded Python installer doesn't match its known checksum - not installing it"
  pkgutil --check-signature "$tmp/python.pkg" 2>/dev/null | grep -q "Python Software Foundation" \
    || fail "the downloaded Python installer isn't signed by the Python Software Foundation - not installing it"
  mm="${PY_PKG_VER%.*}"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><array>'
    for c in PythonApplications PythonUnixTools PythonDocumentation PythonProfileChanges PythonInstallPip PythonTFramework; do
      echo "<dict><key>attributeSetting</key><integer>0</integer><key>choiceAttribute</key><string>selected</string><key>choiceIdentifier</key><string>org.python.Python.$c-$mm</string></dict>"
    done
    echo '</array></plist>'
  } > "$tmp/choices.xml"
  say "Installing Python $PY_PKG_VER (framework only; the python3 command and PATH stay as they are)"
  installer -pkg "$tmp/python.pkg" -applyChoiceChangesXML "$tmp/choices.xml" -target / >/dev/null \
    || fail "the Python installer failed"
  rm -rf "$tmp"
}

say "Checking Python"
if ! find_python; then
  echo "    Python 3.10 or newer isn't installed (Apple's python3 is 3.9)."
  if [ "$AUTO_PY" -eq 1 ] || ask "    Download and install Python $PY_PKG_VER from python.org now? It's installed framework-only and doesn't change the python3 command, so other apps aren't affected."; then
    install_python
    find_python || fail "Python $PY_PKG_VER was installed but doesn't run - see the messages above"
  else
    fail "Python 3.10 or newer is needed. Rerun with --install-python, or install it from https://www.python.org/downloads/macos/ and rerun."
  fi
fi
# resolve the real binary (for the venv and the firewall)
REAL_PY="$(as_user "$PY" -c 'import sys; print(sys.executable)')"
echo "    $("$REAL_PY" --version) at $REAL_PY, $(uname -m)"

# ---- Port ----------------------------------------------------------------------
CFG_SRC="$SRC/config.json"
[ -f "$DEST/config.json" ] && CFG_SRC="$DEST/config.json"
TCP_PORT="$("$REAL_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("tcp_port", 2400))' "$CFG_SRC")"

if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  say "Stopping the running bridge"
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  # bootout returns before the bridge has exited; bootstrapping too early fails with
  # "Bootstrap failed: 5: Input/output error", so wait for both.
  for _ in $(seq 1 30); do
    if ! launchctl print "system/$LABEL" >/dev/null 2>&1 \
       && ! lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
    sleep 1
  done
fi

say "Checking port $TCP_PORT (Savant)"
if lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN
  fail "port $TCP_PORT is already in use. Change tcp_port in config.json (and the port in Blueprint), then rerun."
fi

# ---- Files ---------------------------------------------------------------------
ver() { if [ -f "$1" ]; then sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1" | head -1; fi; return 0; }
OLD_VER="$(ver "$DEST/t10link_bridge.py")"
NEW_VER="$(ver "$SRC/t10link_bridge.py")"
say "Installing T10Link ${NEW_VER} to $DEST${OLD_VER:+ (replacing $OLD_VER)}"
mkdir -p "$DEST/logs"
cp "$SRC/t10link_bridge.py" "$SRC/hap_local.py" "$SRC/t10link_web.py" "$DEST/"
cp "$HERE/t10link" "$HERE/uninstall.sh" "$DEST/"
# the Blueprint profile, for the web page's download link
rm -rf "$DEST/profile"
if [ -d "$HERE/../profile/blueprint-import" ]; then
  mkdir -p "$DEST/profile" && cp -R "$HERE/../profile/blueprint-import/." "$DEST/profile/"
fi
if [ -f "$DEST/config.json" ]; then
  echo "    keeping existing config.json"
else
  BK="$(ls -1d "$USER_HOME"/T10Link-backup-* 2>/dev/null | sort | tail -1 || true)"
  if [ -n "$BK" ] && [ -f "$BK/config.json" ]; then
    for f in "$BK"/config.json "$BK"/pairing*.json "$BK"/sensors*.json "$BK"/setpoints*.json; do [ -f "$f" ] && cp -p "$f" "$DEST/"; done
    echo "    restored settings and pairing from $BK"
  else
    cp "$SRC/config.json" "$DEST/"
    echo "    new config.json: $DEST/config.json"
  fi
fi
chown -R "$RUN_USER" "$DEST"
chmod 700 "$DEST"          # pairing.json (HomeKit keys) lives here
chmod 755 "$DEST/t10link" "$DEST/uninstall.sh"
for f in "$DEST"/pairing*.json; do [ -f "$f" ] && chmod 600 "$f"; done

# ---- Python environment ----------------------------------------------------------
VPY="$DEST/venv/bin/python3"
PYVER='import sys, platform; print("%d.%d %s" % (sys.version_info[0], sys.version_info[1], platform.machine()))'
if [ -x "$VPY" ] && [ "$(as_user "$VPY" -c "$PYVER" 2>/dev/null)" = "$(as_user "$REAL_PY" -c "$PYVER")" ]; then
  echo "    reusing $DEST/venv"
else
  say "Creating Python environment $DEST/venv"
  rm -rf "$DEST/venv"
  as_user "$REAL_PY" -m venv "$DEST/venv"
fi
if ! as_user "$VPY" -I -c "from importlib.metadata import version; assert version('aiohomekit') == '${AIOHOMEKIT#*==}'" 2>/dev/null; then
  say "Installing $AIOHOMEKIT (needs internet; a minute or two)"
  # chacha20poly1305 0.0.3 is pure Python and only published as source; everything else is a
  # prebuilt wheel, so no compiler is needed.
  as_user "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
  as_user "$VPY" -m pip install --quiet --disable-pip-version-check \
    --only-binary=:all: --no-binary=chacha20poly1305 "$AIOHOMEKIT" \
    || fail "installing $AIOHOMEKIT failed - see the messages above"
fi
as_user "$VPY" -I -c "from aiohomekit.controller.ip.controller import IpController" \
  || fail "aiohomekit doesn't import - see the messages above"
as_user "$VPY" -m pip freeze --disable-pip-version-check > "$DEST/requirements.lock"
chown "$RUN_USER" "$DEST/requirements.lock"

CREATED_BIN=0
[ -d /usr/local/bin ] || { mkdir -p /usr/local/bin; CREATED_BIN=1; }
ln -sf "$DEST/t10link" /usr/local/bin/t10link

# ---- launchd ---------------------------------------------------------------------
ARCH_XML=""
[ ${#ARCH[@]} -gt 0 ] && ARCH_XML="<string>/usr/bin/arch</string><string>-arm64</string>"
say "Writing $PLIST"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    $ARCH_XML
    <string>$VPY</string>
    <string>-I</string>
    <string>$DEST/t10link_bridge.py</string>
    <string>-c</string><string>$DEST/config.json</string>
    <string>-l</string><string>$DEST/logs/bridge.log</string>
  </array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>UserName</key><string>$RUN_USER</string>
  <key>RunAtLoad</key><true/>
  <!-- restart after a crash; a clean stop (exit 0) stays stopped -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ExitTimeOut</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$DEST/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DEST/logs/launchd.log</string>
</dict>
</plist>
EOF
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null || fail "plist failed validation"

# ---- Firewall ----------------------------------------------------------------------
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
FW_ADDED=0
if [ -f "$DEST/.install-manifest" ] && grep -q '^FW_ADDED=1' "$DEST/.install-manifest"; then FW_ADDED=1; fi
if [ -x "$FW" ] && "$FW" --getglobalstate | grep -qi "enabled"; then
  if "$FW" --listapps 2>/dev/null | grep -qF "$REAL_PY"; then
    echo "    Python already has a firewall entry - leaving it as is"
  else
    say "macOS firewall is on - allowing incoming connections for Python (Savant connects to port $TCP_PORT)"
    "$FW" --add "$REAL_PY" >/dev/null || true
    "$FW" --unblockapp "$REAL_PY" >/dev/null || true
    FW_ADDED=1
  fi
fi

cat > "$DEST/.install-manifest" <<MANIFEST
# written by install.sh $(date '+%Y-%m-%d %H:%M')
LABEL=$LABEL
PLIST=$PLIST
DEST=$DEST
RUN_USER=$RUN_USER
PYTHON=$REAL_PY
FW_ADDED=$FW_ADDED
CREATED_BIN=$CREATED_BIN
MANIFEST
chown "$RUN_USER" "$DEST/.install-manifest"
echo "$(date '+%Y-%m-%d %H:%M:%S') $NEW_VER" >> "$DEST/install.log"; chown "$RUN_USER" "$DEST/install.log"

# ---- Start and check ---------------------------------------------------------------
say "Starting the bridge"
launchctl enable "system/$LABEL" 2>/dev/null || true
for try in 1 2 3 4 5; do
  launchctl bootstrap system "$PLIST" 2>/dev/null && break
  [ "$try" -eq 5 ] && launchctl bootstrap system "$PLIST"   # last try: let the error show
  sleep 2
done
ok=0
for _ in $(seq 1 20); do
  if lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  tail -20 "$DEST/logs/bridge.log" "$DEST/logs/launchd.log" 2>/dev/null || true
  fail "the bridge did not start - see the logs above"
fi

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<this Mac'"'"'s IP>')"
WEB_PORT="$(sed -n 's/.*"web_port": *\([0-9]*\).*/\1/p' "$DEST/config.json" | head -1)"
WEB_PORT="${WEB_PORT:-2401}"
URL="http://$IP:$WEB_PORT"
say "Installed and running"
cat <<EOF

    Bridge version:  $NEW_VER
    Web page:        $URL
    Savant profile:  Device IP Address $IP, port $TCP_PORT
    Control:         t10link status | states | logs | sudo t10link restart
    Remove:          sudo $DEST/uninstall.sh

EOF
if [ "$WEB_PORT" != "0" ] && ! ls "$DEST"/pairing*.json >/dev/null 2>&1; then
  cat <<EOF
  Next, on the web page ($URL, from any computer or phone on this network):
    1. Reserve each thermostat's IP address in the router (DHCP reservation).
    2. Search the network, add the thermostat, and pair it with the code from
       Menu > Connect HomeKit on the thermostat.
    3. Download the Blueprint profile there and import it: Device IP Address $IP, port $TCP_PORT.

EOF
fi
# open the page when someone is at this Mac: a local Terminal session (who -m shows "(host)" for
# ssh, and sudo drops SSH_CONNECTION) of the user logged in at the screen
if [ "$OPEN_PAGE" -eq 1 ] && [ "$WEB_PORT" != "0" ] && [ -r /dev/tty ] \
   && WHO="$(who -m < /dev/tty 2>/dev/null)" && [ -n "$WHO" ] && ! printf '%s' "$WHO" | grep -q '(' \
   && [ "$(stat -f %Su /dev/console 2>/dev/null)" = "$RUN_USER" ]; then
  sudo -u "$RUN_USER" open "http://localhost:$WEB_PORT" >/dev/null 2>&1 || true
fi
