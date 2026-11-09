#!/bin/bash
# T10Link one-line install: downloads the latest release from GitHub, checks its SHA-256, unzips
# it into ~/T10Link-installer/<version> and runs its installer.
#
#   curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash -s -- --user RPM
#
# Anything after "--" goes to the installer (see install.sh).
set -euo pipefail
REPO="jwheeler188/T10Link"
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
command -v curl >/dev/null || fail "curl is needed"

# the latest release's tag, from the redirect of /releases/latest (no API or JSON needed)
TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")"
TAG="${TAG##*/}"
case "$TAG" in v[0-9]*) ;; *) fail "couldn't find the latest T10Link release (got '$TAG')" ;; esac
VER="${TAG#v}"
ZIP="T10Link-$VER.zip"
BASE="https://github.com/$REPO/releases/download/$TAG"
DIR="$HOME/T10Link-installer/$VER"

say "Downloading T10Link $VER"
rm -rf "$DIR"
mkdir -p "$DIR"
curl -fL --retry 3 --progress-bar -o "$DIR/$ZIP" "$BASE/$ZIP"
WANT="$(curl -fsSL "$BASE/$ZIP.sha256" | awk '{print $1}')"
if command -v shasum >/dev/null; then GOT="$(shasum -a 256 "$DIR/$ZIP" | awk '{print $1}')"
else GOT="$(sha256sum "$DIR/$ZIP" | awk '{print $1}')"; fi
[ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || fail "checksum mismatch for $ZIP (expected $WANT, got $GOT)"
echo "    SHA-256 OK"

if command -v unzip >/dev/null; then unzip -q "$DIR/$ZIP" -d "$DIR"
else python3 -m zipfile -e "$DIR/$ZIP" "$DIR"; fi
chmod +x "$DIR"/T10Link/install.sh "$DIR"/T10Link/macos/*.sh "$DIR"/T10Link/linux/*.sh 2>/dev/null || true
exec /bin/bash "$DIR/T10Link/install.sh" "$@"
