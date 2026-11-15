#!/bin/bash
# What is running right now.
SUPPORT="$HOME/Library/Application Support/VoiceType"
echo "== app =="
pgrep -fl "VoiceType.app/Contents/MacOS/VoiceType" || echo "  未运行"
echo "== engine =="
pgrep -fl "engine.py --serve" || echo "  未运行"
echo "== socket =="
ls -la "$SUPPORT/engine.sock" 2>/dev/null || echo "  无"
echo "== ping =="
python3 - <<PY
import json, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3)
try:
    s.connect("$SUPPORT/engine.sock")
    s.sendall(b'{"cmd":"ping"}\n')
    print(" ", s.recv(4096).decode().strip())
except Exception as e:
    print("  引擎无响应:", e)
PY
echo "== 最近日志 =="
tail -8 "$SUPPORT/voicetype.log" 2>/dev/null || echo "  无日志"
