#!/bin/bash
# Compiles the menu bar app into ../bin/VoiceType.app
set -euo pipefail
cd "$(dirname "$0")"

APP="$PWD/../bin/VoiceType.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
[ -f icon/AppIcon.icns ] || ./icon/make-icns.sh
cp icon/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

swiftc -O -whole-module-optimization \
  -target arm64-apple-macos13.0 \
  -framework AppKit -framework AVFoundation -framework Carbon \
  Sources/*.swift \
  -o "$APP/Contents/MacOS/VoiceType"

# 用本地自签名证书签名：辅助功能授权会跟着这张证书走，重新编译不再让它失效。
# 没有证书时退回 ad-hoc —— 能跑，但每次重编译都要用户重新授权。
IDENTITY="VoiceType Local Signing"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
  codesign --force --deep --sign "$IDENTITY" "$APP" >/dev/null 2>&1 \
    || echo "warning: 证书签名失败，回退 ad-hoc"
else
  codesign --force --sign - "$APP" >/dev/null 2>&1 \
    || echo "warning: ad-hoc codesign failed"
  echo "note: 没有本地签名证书（见 app/make-signing-identity.sh），本次为 ad-hoc 签名"
fi
echo "built: $APP"
