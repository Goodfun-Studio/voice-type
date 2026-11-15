"""Client for the transcription engine, over loopback TCP.

Same frozen wire protocol as the macOS EngineClient, only the transport differs
(unix socket -> 127.0.0.1 TCP). One JSON object per line, one reply per line.

Spawn command (explicit TCP on Windows):

    python engine.py --serve --transport tcp --port 8765

Endpoint discovery: the engine publishes %APPDATA%/VoiceType/engine.json; we read
it for host/port and fall back to the configured defaults if it is missing.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfg
from .config import Config, log_line

CREATE_NO_WINDOW = 0x08000000  # Windows only; keeps a console from flashing up


class EngineError(RuntimeError):
    pass


class EngineClient:
    def __init__(self, config: Config):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---- language -----------------------------------------------------------
    def set_language(self, code: str) -> None:
        with self._lock:
            self.config.language = code

    # ---- transport ----------------------------------------------------------
    def _endpoint(self) -> tuple[str, int]:
        with self._lock:
            return self.config.endpoint()

    def _request(self, payload: dict, timeout: float) -> dict:
        host, port = self._endpoint()
        deadline = time.monotonic() + timeout
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineError("timed out waiting for engine")
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(65536)
                except socket.timeout as exc:
                    raise EngineError("timed out waiting for engine") from exc
                if not chunk:
                    break
                buffer += chunk
        line = buffer.split(b"\n", 1)[0].strip()
        if not line:
            raise EngineError("empty response from engine")
        try:
            obj = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"bad response from engine: {exc}") from exc
        if isinstance(obj, dict) and obj.get("error"):
            raise EngineError(str(obj["error"]))
        return obj

    def ping(self, timeout: float = 1.2) -> bool:
        try:
            self._request({"cmd": "ping"}, timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def transcribe(self, wav: str, language: str | None = None) -> dict:
        """Returns the engine payload. Note: an ``ok`` payload with empty text and
        a ``reason`` (silence / hallucination / no-words / prompt-echo) is a
        normal result, not an error — the caller must not paste it."""
        lang = language if language else self.config.language
        return self._request({"cmd": "transcribe", "wav": wav, "language": lang},
                             timeout=180)

    # ---- lifecycle ----------------------------------------------------------
    def ensure_running(self, wait: float = 90.0) -> bool:
        if self.ping():
            return True
        log_line("engine not reachable, spawning")
        if not self._spawn():
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.ping():
                log_line("engine is up")
                return True
            time.sleep(0.6)
        log_line("engine did not come up in time")
        return False

    def _spawn(self) -> bool:
        python = self.config.python or sys.executable
        engine = self.config.engine
        if not python or not engine or not Path(engine).exists():
            log_line(f"cannot spawn engine (python={python!r} engine={engine!r})")
            return False

        cfg.ensure_dirs()
        env = dict(os.environ)
        env["VOICETYPE_TRANSPORT"] = "tcp"
        env["VOICETYPE_PORT"] = str(self.config.port)
        env["VOICETYPE_HOST"] = self.config.host
        # Only set these when non-empty so the engine's own defaults win otherwise.
        if self.config.device:
            env["VOICETYPE_DEVICE"] = self.config.device
        if self.config.compute_type:
            env["VOICETYPE_COMPUTE_TYPE"] = self.config.compute_type
        if self.config.model:
            env["VOICETYPE_FW_MODEL"] = self.config.model
        if self.config.beam_size:
            env["VOICETYPE_BEAM"] = str(self.config.beam_size)

        args = [python, engine, "--serve", "--transport", "tcp", "--port", str(self.config.port)]
        kwargs: dict = {"env": env, "cwd": str(Path(engine).parent)}
        try:
            log_handle = open(cfg.LOG_PATH, "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            log_handle = subprocess.DEVNULL  # type: ignore[assignment]
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log_line(f"failed to spawn engine: {exc}")
            return False
        log_line(f"spawned engine pid={self._process.pid}")
        return True

    def shutdown(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:  # noqa: BLE001
                pass
