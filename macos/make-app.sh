#!/usr/bin/env bash
# Builds Ixel.app, Ixel's own window on a Mac, into a folder (install.sh uses ~/Applications).
#   macos/make-app.sh <Ixel's python> <folder> [PATH for it to add]
# With the Command Line Tools (xcode-select --install) it's a native app (main.swift). Without them it
# opens Ixel in Chrome or Edge's app mode instead. Prints which ("native" or "browser"), then where it went.
# It never replaces an app that isn't its own: if the folder already has another Ixel.app, this one is
# "Ixel MAT.app" instead (exit 3 if that name is taken by another app too).
set -euo pipefail

PYTHON="$1"
DEST="$2"
EXTRA_PATH="${3:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$("$PYTHON" -I -c 'import ixel_mat; print(ixel_mat.__version__)' 2>/dev/null || echo 0)"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
APP="$BUILD/Ixel.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/../ixel_mat/assets/ixel.icns" "$APP/Contents/Resources/ixel.icns"
plutil -replace IxelPython -string "$PYTHON" "$APP/Contents/Info.plist"
plutil -replace IxelPath -string "$EXTRA_PATH" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"

# xcode-select -p first: without the tools, /usr/bin/swiftc only offers to install them
if xcode-select -p >/dev/null 2>&1 && \
   swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/Ixel" "$HERE/Ixel/main.swift" >"$BUILD/swiftc.log" 2>&1; then
  KIND=native
else
  # Through a login shell, as the native app does, so your model CLIs are on PATH
  cat > "$APP/Contents/MacOS/Ixel" <<SCRIPT
#!/bin/sh
PATH="$EXTRA_PATH:\$PATH"; export PATH
exec /bin/zsh -l -c 'exec "\$0" -I -m ixel_mat app --browser' "$PYTHON"
SCRIPT
  chmod +x "$APP/Contents/MacOS/Ixel"
  KIND=browser
fi
# One this script made: its bundle identifier is the one in Info.plist here (read by Ixel's python)
ours() {
  "$PYTHON" -I -c 'import plistlib, sys
def bundle(path):
    with open(path, "rb") as f:
        return plistlib.load(f).get("CFBundleIdentifier")
try:
    sys.exit(bundle(sys.argv[1]) != bundle(sys.argv[2]))
except Exception:
    sys.exit(1)' "$1/Contents/Info.plist" "$HERE/Info.plist" 2>/dev/null
}
NAME=Ixel
if [[ -e "$DEST/Ixel.app" || -L "$DEST/Ixel.app" ]] && ! ours "$DEST/Ixel.app"; then
  NAME="Ixel MAT"
  if [[ -e "$DEST/$NAME.app" || -L "$DEST/$NAME.app" ]] && ! ours "$DEST/$NAME.app"; then
    echo "error: $DEST already has apps called Ixel.app and $NAME.app that aren't Ixel's; left them as they are" >&2
    exit 3
  fi
  plutil -replace CFBundleName -string "$NAME" "$APP/Contents/Info.plist"
  plutil -replace CFBundleDisplayName -string "$NAME" "$APP/Contents/Info.plist"
fi
# Signed for this Mac only ("-"): built here, so Gatekeeper has nothing to check
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

mkdir -p "$DEST"
rm -rf "$DEST/$NAME.app"
mv "$APP" "$DEST/$NAME.app"
# Back under its own name: the Ixel MAT.app an earlier install made beside another Ixel.app goes
if [[ "$NAME" == Ixel ]] && ours "$DEST/Ixel MAT.app"; then
  rm -rf "$DEST/Ixel MAT.app"
fi
echo "$KIND"
echo "$DEST/$NAME.app"
