#!/usr/bin/env python3
"""Voice Type transcription daemon.

Keeps a whisper model warm in memory and answers transcription requests over a
local socket. One protocol, two transports:

  unix socket  — macOS default; what the shipped Swift app talks to
  127.0.0.1 TCP — Windows (and anywhere unix sockets are awkward)

Protocol: one JSON object per line, one request per line, one reply per line.

  {"cmd": "ping"}
  {"cmd": "transcribe", "wav": "C:\\tmp\\x.wav", "language": "zh"|"auto"}

The speech model itself lives behind backends.py: Apple MLX on M-series Macs,
CTranslate2 (faster-whisper) everywhere else. Everything below the backend —
silence gate, hallucination filter, cleanup rules, vocabulary rewrite — is
shared by both.

Run standalone for one-off files:  engine.py --once file.wav
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
from backends import Backend, choose_backend, ensure_hf_endpoint  # noqa: E402
from cleanup import (apply_vocabulary, clean, has_speech,  # noqa: E402
                      is_hallucination, is_prompt_echo, is_repetition_loop)

# A launchd-launched app inherits a bare PATH, so mlx_whisper's ffmpeg shell-out
# would fail there even though ffmpeg is installed. Make the usual bin dirs visible.
for _extra in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _extra not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _extra + ":" + os.environ.get("PATH", "")

def _support_dir() -> Path:
    """Where this tool keeps its own files, per platform. Overridable with
    VOICETYPE_DIR so tests and the Windows installer can redirect it."""
    override = os.environ.get("VOICETYPE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "VoiceType"
    return Path.home() / "Library/Application Support/VoiceType"


SUPPORT = _support_dir()
SOCK = Path(os.environ.get("VOICETYPE_SOCK", SUPPORT / "engine.sock"))
# Clients read this to discover how to reach a running engine, so they never
# have to guess the transport or port.
ENDPOINT_FILE = SUPPORT / "engine.json"
DEFAULT_HOST = os.environ.get("VOICETYPE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("VOICETYPE_PORT", "8765"))

# Picked once at startup; see backends.choose_backend().
BACKEND: Backend = choose_backend()
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
        log(f"loading {BACKEND.describe()}")
        started = time.time()
        # Running a scrap of silence pays the one-off cost (Metal kernel
        # compile / graph warm-up) now instead of on the first real utterance.
        BACKEND.load(_write_silence())
        _model_loaded = True
        log(f"model warm in {time.time() - started:.1f}s ({BACKEND.describe()})")


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

    duration, rms = audio_stats(path)
    if duration < 0.2 or rms < SILENCE_RMS:
        return {"text": "", "raw": "", "reason": "silence",
                "audioSeconds": round(duration, 2), "rms": round(rms, 5)}

    started = time.time()
    result = BACKEND.transcribe(
        path,
        None if language in ("auto", "", None) else language,
        build_prompt(language),
    )
    elapsed = time.time() - started
    raw = (result.get("text") or "").strip()

    # A repeat loop is a failed decode, not something to paste.
    if is_repetition_loop(raw):
        log(f"dropped repetition loop: {raw[:40]!r}…")
        return {
            "text": "", "raw": raw, "reason": "repetition",
            "elapsedMs": int(elapsed * 1000),
            "audioSeconds": round(duration, 2),
            "rms": round(rms, 5),
        }

    # Parroting the decoder prompt back is a real failure, mostly at beam_size=1.
    if is_prompt_echo(raw, build_prompt(language)):
        log(f"dropped prompt echo: {raw[:40]!r}")
        return {
            "text": "", "raw": raw, "reason": "prompt-echo",
            "elapsedMs": int(elapsed * 1000),
            "audioSeconds": round(duration, 2),
            "rms": round(rms, 5),
        }

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

    # A bare "……" is not something to paste into someone's document.
    if not has_speech(text):
        log(f"dropped wordless transcript (rms={rms:.5f}): {text[:20]!r}")
        return {
            "text": "", "raw": raw, "reason": "no-words",
            "elapsedMs": int(elapsed * 1000),
            "audioSeconds": round(duration, 2),
            "rms": round(rms, 5),
        }

    # whisper sometimes only emits a subtitle-style credit on noise.
    if text and len(text) < 30 and "字幕" in text and "提供" in text:
        text = ""
    return {
        "text": text,
        "raw": raw,
        "language": result.get("language"),
        "backend": BACKEND.describe(),
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
                self._reply({"ok": True, "model": BACKEND.describe(),
                             "backend": BACKEND.name, "warm": _model_loaded})
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
        # The client may have gone away mid-request (a probe that times out, the
        # app restarting). A dead pipe is not an engine error, so don't spray
        # tracebacks into the log over it.
        try:
            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Without this, Windows can keep the listening port unusable for a while
    # after a crash. The explicit probe in serve() is what actually prevents a
    # double start, so allowing reuse here costs nothing.
    allow_reuse_address = True


def write_endpoint(transport: str, host: str, port: int | None) -> None:
    """Publish how to reach this engine so clients never have to guess the
    transport or port."""
    payload = {"transport": transport, "backend": BACKEND.name, "pid": os.getpid()}
    if transport == "unix":
        payload["socket"] = str(SOCK)
    else:
        payload["host"] = host
        payload["port"] = port
    ENDPOINT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _already_running(transport: str, host: str, port: int | None) -> bool:
    family = socket.AF_UNIX if transport == "unix" else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(SOCK) if transport == "unix" else (host, port))
        return True
    except Exception:  # noqa: BLE001
        if transport == "unix":
            SOCK.unlink(missing_ok=True)   # leftover from a crash
        return False
    finally:
        probe.close()


VOCAB_HEADER = """# 一行一个词。专有名词/人名/内部术语放这里，识别时会按拼音纠正同音误听。
"""


def ensure_support_files() -> None:
    """Create our own directory and a starter vocabulary file. Doing this here
    rather than only in the installer means a fresh machine (or a test dir)
    still gets the homophone-correction behaviour instead of silently using an
    empty word list."""
    SUPPORT.mkdir(parents=True, exist_ok=True)
    if not VOCAB_FILE.exists():
        VOCAB_FILE.write_text(VOCAB_HEADER, encoding="utf-8")


def serve(transport: str = "unix", host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> None:
    ensure_support_files()
    if _already_running(transport, host, port):
        log(f"another engine already listening ({transport}), exiting")
        return

    if transport == "unix":
        server = UnixServer(str(SOCK), Handler)
        where = f"unix:{SOCK}"
    else:
        server = TcpServer((host, port), Handler)
        where = f"tcp:{host}:{port}"

    # Only publish the endpoint once the socket is actually bound.
    write_endpoint(transport, host, None if transport == "unix" else port)
    log(f"serving on {where} · {BACKEND.describe()}")
    load_model()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if transport == "unix":
            SOCK.unlink(missing_ok=True)
        ENDPOINT_FILE.unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--once":
        target = args[1]
        language = args[2] if len(args) > 2 else "zh"
        payload = transcribe(target, language)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # Windows has no unix sockets, so it defaults to loopback TCP.
    transport = os.environ.get("VOICETYPE_TRANSPORT") or (
        "tcp" if sys.platform == "win32" else "unix")
    host, port = DEFAULT_HOST, DEFAULT_PORT
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--serve":
            continue
        if flag == "--transport" and rest:
            transport = rest.pop(0)
        elif flag == "--host" and rest:
            host = rest.pop(0)
        elif flag == "--port" and rest:
            port = int(rest.pop(0))
        else:
            print(f"unknown argument: {flag}", file=sys.stderr)
            raise SystemExit(2)
    if transport not in ("unix", "tcp"):
        print(f"unknown transport: {transport}", file=sys.stderr)
        raise SystemExit(2)
    serve(transport, host, port)


if __name__ == "__main__":
    main()
