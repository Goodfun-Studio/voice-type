#!/bin/bash
# Compiles the menu bar app into ../bin/VoiceType.app
set -euo pipefail
cd "$(dirname "$0")"

APP="$PWD/../bin/VoiceType.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"

swiftc -O -whole-module-optimization \
  -target arm64-apple-macos13.0 \
  -framework AppKit -framework AVFoundation -framework Carbon \
  Sources/*.swift \
  -o "$APP/Contents/MacOS/VoiceType"

codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "warning: ad-hoc codesign failed"
echo "built: $APP"
