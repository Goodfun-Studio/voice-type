#!/usr/bin/env python3
"""Voice Type transcription daemon.

Holds whisper-large-v3-turbo (MLX) warm in memory and answers transcription
requests over a unix socket. Protocol: one JSON object per line, request then
one JSON object per line as the response.

  {"cmd": "ping"}
  {"cmd": "transcribe", "wav": "/tmp/x.wav", "language": "zh"|"auto"}

Run standalone for one-off files with:  engine.py --once file.wav
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup import apply_vocabulary, clean, is_hallucination  # noqa: E402

def _pick_hf_endpoint() -> None:
    """huggingface.co is unreachable on some networks (and hard-blocked on
    others); fall back to the mirror when a 2s probe says so."""
    if os.environ.get("HF_ENDPOINT"):
        return
    import socket as _socket
    try:
        probe = _socket.create_connection(("huggingface.co", 443), timeout=2.5)
        probe.close()
    except OSError:
        os.environ["HF_ENDPOINT"] = os.environ.get(
            "VOICETYPE_HF_MIRROR", "https://hf-mirror.com")
        print("huggingface.co unreachable, using", os.environ["HF_ENDPOINT"], flush=True)


# A launchd-launched app inherits a bare PATH, so mlx_whisper's ffmpeg shell-out
# would fail there even though ffmpeg is installed. Make the usual bin dirs visible.
for _extra in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _extra not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _extra + ":" + os.environ.get("PATH", "")

SUPPORT = Path.home() / "Library/Application Support/VoiceType"
SOCK = Path(os.environ.get("VOICETYPE_SOCK", SUPPORT / "engine.sock"))
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "models/whisper-large-v3-turbo"
LOCAL_WEIGHTS = DEFAULT_MODEL_DIR / "weights.safetensors"


def _local_model_ready() -> bool:
    # Guard against a half-finished download sitting in the folder.
    return LOCAL_WEIGHTS.exists() and LOCAL_WEIGHTS.stat().st_size > 100_000_000


MODEL = os.environ.get(
    "VOICETYPE_MODEL",
    str(DEFAULT_MODEL_DIR) if _local_model_ready() else "mlx-community/whisper-large-v3-turbo",
)
# Biasing the decoder toward Simplified Chinese stops whisper drifting into
# Traditional characters, which it otherwise does on Mandarin audio.
INITIAL_PROMPT = os.environ.get(
    "VOICETYPE_INITIAL_PROMPT",
    "以下是普通话的句子，请使用简体中文和正确的标点符号。",
)
SILENCE_RMS = 0.0018
VOCAB_FILE = SUPPORT / "vocabulary.txt"

_vocab_cache: tuple[float, list[str]] = (0.0, [])


def vocabulary() -> list[str]:
    """Domain words whispered into the decoder so proper nouns survive
    (e.g. 识鱼 otherwise comes back as 食鱼). One term per line."""
    if not VOCAB_FILE.exists():
        return []
    mtime = VOCAB_FILE.stat().st_mtime
    cached_mtime, cached = _vocab_cache
    if mtime == cached_mtime:
        return cached
    terms = [line.strip() for line in VOCAB_FILE.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    globals()["_vocab_cache"] = (mtime, terms)
    return terms


def build_prompt(language: str | None) -> str | None:
    base = "" if language == "en" else INITIAL_PROMPT
    terms = vocabulary()
    if terms:
        base = (base + " " if base else "") + "常见词：" + "、".join(terms) + "。"
    return base or None

_model_lock = threading.Lock()
_model_loaded = False


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def load_model() -> None:
    global _model_loaded
    if _model_loaded:
        return
    with _model_lock:
        if _model_loaded:
            return
        _pick_hf_endpoint()
        import mlx_whisper  # noqa: F401  (import cost is part of warm-up)
        log(f"loading {MODEL}")
        started = time.time()
        # Running a scrap of silence forces Metal kernels to compile now rather
        # than on the user's first real utterance.
        mlx_whisper.transcribe(
            str(_write_silence()), path_or_hf_repo=MODEL, language="zh",
            initial_prompt=INITIAL_PROMPT, verbose=None,
        )
        _model_loaded = True
        log(f"model warm in {time.time() - started:.1f}s")


def _write_silence(seconds: float = 1.0) -> Path:
    path = SUPPORT / "warmup.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def audio_stats(path: str) -> tuple[float, float]:
    """Return (duration_seconds, rms) for a 16-bit PCM wav."""
    with wave.open(path, "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        raw = handle.readframes(frames)
    if not raw:
        return 0.0, 0.0
    import array
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return frames / rate, 0.0
    total = sum(float(s) * float(s) for s in samples) / len(samples)
    return frames / rate, (total ** 0.5) / 32768.0


def transcribe(path: str, language: str = "zh", polish: bool = True) -> dict:
    load_model()
    import mlx_whisper

    duration, rms = audio_stats(path)
    if duration < 0.2 or rms < SILENCE_RMS:
        return {"text": "", "raw": "", "reason": "silence",
                "audioSeconds": round(duration, 2), "rms": round(rms, 5)}

    started = time.time()
    result = mlx_whisper.transcribe(
        path,
        path_or_hf_repo=MODEL,
        language=None if language in ("auto", "", None) else language,
        initial_prompt=build_prompt(language),
        temperature=0.0,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        verbose=None,
    )
    elapsed = time.time() - started
    raw = (result.get("text") or "").strip()

    # whisper recites stock subtitle credits when the clip is just room noise.
    if is_hallucination(raw, rms):
        log(f"dropped hallucination on quiet audio (rms={rms:.5f}): {raw[:40]}")
        return {
            "text": "", "raw": raw, "reason": "hallucination",
            "elapsedMs": int(elapsed * 1000),
            "audioSeconds": round(duration, 2),
            "rms": round(rms, 5),
        }

    text = clean(raw) if polish else raw
    if polish:
        text = apply_vocabulary(text, vocabulary())

    # whisper sometimes only emits a subtitle-style credit on noise.
    if text and len(text) < 30 and "字幕" in text and "提供" in text:
        text = ""
    return {
        "text": text,
        "raw": raw,
        "language": result.get("language"),
        "elapsedMs": int(elapsed * 1000),
        "audioSeconds": round(duration, 2),
        "rms": round(rms, 5),
        "rtf": round(elapsed / duration, 3) if duration else None,
    }


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        try:
            request = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._reply({"error": f"bad request: {exc}"})
            return

        cmd = request.get("cmd")
        try:
            if cmd == "ping":
                self._reply({"ok": True, "model": MODEL, "warm": _model_loaded})
            elif cmd == "transcribe":
                payload = transcribe(
                    request["wav"],
                    request.get("language", "zh"),
                    request.get("polish", True),
                )
                payload["ok"] = True
                self._reply(payload)
            else:
                self._reply({"error": f"unknown cmd: {cmd}"})
        except Exception as exc:  # noqa: BLE001
            log(f"error: {exc!r}")
            self._reply({"error": str(exc)})

    def _reply(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve() -> None:
    SUPPORT.mkdir(parents=True, exist_ok=True)
    if SOCK.exists():
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            probe.connect(str(SOCK))
            probe.close()
            log(f"another engine already listening on {SOCK}, exiting")
            return
        except Exception:  # noqa: BLE001
            SOCK.unlink(missing_ok=True)

    log(f"serving on {SOCK}")
    load_model()
    server = Server(str(SOCK), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        SOCK.unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--once":
        target = args[1]
        language = args[2] if len(args) > 2 else "zh"
        print(json.dumps(transcribe(target, language), ensure_ascii=False, indent=2))
        return
    serve()


if __name__ == "__main__":
    main()
