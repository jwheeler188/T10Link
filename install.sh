#!/bin/bash
# T10Link installer: runs the Mac or the Linux / Raspberry Pi installer for this machine.
# From the unzipped T10Link folder:
#
#   ./install.sh                  install or upgrade (asks for your password when it needs sudo)
#   ./install.sh --user RPM       Mac: run the service as another user (e.g. RPM on a Savant host)
#
# Or straight from GitHub, which downloads the latest release first:
#   curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(uname)" in
  Darwin)
    if [ "$(id -u)" -eq 0 ]; then exec /bin/bash "$HERE/macos/install.sh" "$@"; fi
    echo "T10Link installs a system service, so it needs your Mac password (sudo)."
    exec sudo /bin/bash "$HERE/macos/install.sh" "$@"
    ;;
  Linux)
    exec sh "$HERE/linux/install.sh" "$@"
    ;;
  *)
    echo "T10Link runs on macOS or Linux (Raspberry Pi OS, Debian, Ubuntu)." >&2
    exit 1
    ;;
esac
