#!/bin/bash
# One-shot setup: python env, config, app build, login autostart.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
SUPPORT="$HOME/Library/Application Support/VoiceType"
# pypi.org is slow on this network and tuna 403s some wheels; aliyun is fast here.
INDEX="${VOICETYPE_PYPI:-https://mirrors.aliyun.com/pypi/simple/}"
mkdir -p "$SUPPORT"

echo "==> 1/4 python env"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  uv venv --python 3.12 "$ROOT/.venv"
fi
uv pip install --python "$ROOT/.venv/bin/python" --index-url "$INDEX" \
  mlx-whisper opencc-python-reimplemented pypinyin

echo "==> 2/4 model"
MODEL_DIR="$ROOT/models/whisper-large-v3-turbo"
if [ "$(stat -f%z "$MODEL_DIR/weights.safetensors" 2>/dev/null || echo 0)" -lt 100000000 ]; then
  mkdir -p "$MODEL_DIR"
  BASE="${VOICETYPE_HF_MIRROR:-https://hf-mirror.com}/mlx-community/whisper-large-v3-turbo/resolve/main"
  curl -sL -o "$MODEL_DIR/config.json" "$BASE/config.json"
  curl -L -C - --retry 3 -o "$MODEL_DIR/weights.safetensors" "$BASE/weights.safetensors"
fi
echo "    model: $(du -sh "$MODEL_DIR" | cut -f1)"

echo "==> 3/4 config"
cat > "$SUPPORT/config.json" <<JSON
{
  "python": "$ROOT/.venv/bin/python",
  "engine": "$ROOT/engine/engine.py",
  "socket": "$SUPPORT/engine.sock",
  "language": "zh",
  "sound": true
}
JSON
if [ ! -f "$SUPPORT/vocabulary.txt" ]; then
  cat > "$SUPPORT/vocabulary.txt" <<'VOCAB'
# 一行一个词。专有名词/人名/内部术语放这里，识别时会按拼音纠正同音误听。
VOCAB
fi

echo "==> 4/4 build + autostart"
"$ROOT/app/build.sh"

LA="$HOME/Library/LaunchAgents/com.goodfunstudio.voicetype.plist"
mkdir -p "$(dirname "$LA")"
cat > "$LA" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.goodfunstudio.voicetype</string>
  <key>Program</key><string>$ROOT/bin/VoiceType.app/Contents/MacOS/VoiceType</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardErrorPath</key><string>$SUPPORT/voicetype.log</string>
</dict>
</plist>
PLIST
launchctl unload "$LA" 2>/dev/null || true
launchctl load "$LA"

echo
echo "装好了。"
echo "  菜单栏麦克风图标 = 引擎就绪"
echo "  双击 Fn 开始/结束，备选 ⌥空格，Esc 不受影响"
echo "  还要手动给 VoiceType 两个权限：辅助功能 + 麦克风"
echo "  术语表：$SUPPORT/vocabulary.txt"
