#!/bin/bash
# 从 app/icon/AppIcon-1024-rounded.png 生成 AppIcon.icns
# （生成的 .icns 不进版本库 —— 需要时跑这个脚本，build.sh 也会在缺失时自动调用）
set -euo pipefail
cd "$(dirname "$0")"
SRC=AppIcon-1024-rounded.png
[ -f "$SRC" ] || { echo "缺少 $SRC"; exit 1; }
rm -rf AppIcon.iconset && mkdir AppIcon.iconset
for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
            "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" "512 icon_256x256@2x" \
            "512 icon_512x512" "1024 icon_512x512@2x"; do
  set -- $spec
  (cd AppIcon.iconset && sips -s format png -z "$1" "$1" "../$SRC" --out "$2.png" >/dev/null)
done
iconutil -c icns AppIcon.iconset -o AppIcon.icns
rm -rf AppIcon.iconset
echo "已生成 AppIcon.icns"
