#!/bin/bash
# End-to-end check that does not need a microphone: synthesize speech, transcribe it.
set -euo pipefail
cd "$(dirname "$0")"
TEXT="${1:-嗯，我今天下午三点跟产品开个会，然后我们把识鱼那个流程再对一遍。}"
AIFF=$(mktemp -t vt).aiff
WAV=$(mktemp -t vt).wav
say -v Tingting "$TEXT" -o "$AIFF"
ffmpeg -y -loglevel error -i "$AIFF" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV"
echo "输入: $TEXT"
echo "--- 转写 ---"
.venv/bin/python engine/engine.py --once "$WAV" zh
rm -f "$AIFF" "$WAV"
