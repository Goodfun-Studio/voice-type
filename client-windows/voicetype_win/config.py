"""Config + filesystem layout for the Windows client.

Mirrors the macOS layout, but rooted at %APPDATA%/VoiceType (same place the
engine uses on Windows — see engine/engine.py::_support_dir):

    %APPDATA%/VoiceType/
        config.json      # this module
        engine.json      # published by the engine (endpoint discovery)
        history/         # YYYY-MM-DD.md
        voicetype.log

Everything here is import-safe off-Windows: paths just resolve to an APPDATA-like
location so --selftest and syntax checks work on the Mac.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "VoiceType"

# client-windows/voicetype_win/config.py -> repo root is parents[2]
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
DEFAULT_ENGINE = _REPO_ROOT / "engine" / "engine.py"


def _appdata_base() -> Path:
    """%APPDATA% on Windows. Off-Windows (dev on the Mac) fall back to the same
    shape so nothing is written into the repo and --selftest stays side-effect
    free from the user's point of view."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base)
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming"
    # Non-Windows: keep it out of the way but deterministic.
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def support_dir() -> Path:
    override = os.environ.get("VOICETYPE_DIR")
    if override:
        return Path(override)
    return _appdata_base() / APP_NAME


SUPPORT = support_dir()
CONFIG_PATH = SUPPORT / "config.json"
ENDPOINT_PATH = SUPPORT / "engine.json"     # written by the engine, read by us
HISTORY_DIR = SUPPORT / "history"
LOG_PATH = SUPPORT / "voicetype.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass
class Config:
    python: str = ""            # interpreter that can import faster-whisper
    engine: str = ""            # path to engine/engine.py
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    transport: str = "tcp"      # Windows is always tcp
    model: str = "small"        # faster-whisper size; "large-v3-turbo" on cuda
    device: str = "cpu"         # "cpu" | "cuda"
    compute_type: str = "int8"  # "int8" on cpu, "float16" on cuda
    language: str = "zh"        # "zh" | "en" | "auto"
    # beam=1 makes whisper parrot the decoder prompt back; the engine guards
    # against that but the right fix is to never ask for beam 1. Keep 5.
    beam_size: int = 5
    sound: bool = True

    # ---- defaults for the machine this runs on ----
    @classmethod
    def defaults(cls) -> "Config":
        python = os.environ.get("VOICETYPE_PYTHON") or sys.executable or ""
        engine = os.environ.get("VOICETYPE_ENGINE") or str(DEFAULT_ENGINE)
        return cls(python=python, engine=engine)

    @classmethod
    def load(cls, *, create: bool = True) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                base = cls.defaults()
                for key, value in data.items():
                    if hasattr(base, key):
                        setattr(base, key, value)
                return base
            except Exception as exc:  # noqa: BLE001
                log_line(f"config.json unreadable ({exc}); using defaults")
        cfg = cls.defaults()
        if create:
            try:
                cfg.save()
            except Exception as exc:  # noqa: BLE001
                log_line(f"could not write default config: {exc}")
        return cfg

    def save(self) -> None:
        SUPPORT.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ---- endpoint discovery ----
    def endpoint(self) -> tuple[str, int]:
        """host/port to talk to. Prefers the engine's own engine.json (it may
        have bound a different port), falls back to the configured defaults."""
        try:
            data = json.loads(ENDPOINT_PATH.read_text(encoding="utf-8"))
            if data.get("transport", "tcp") == "tcp" and data.get("host"):
                return str(data["host"]), int(data["port"])
        except Exception:  # noqa: BLE001
            pass
        return self.host, self.port


def read_endpoint() -> dict | None:
    try:
        return json.loads(ENDPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def ensure_dirs() -> None:
    SUPPORT.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def log_line(message: str) -> None:
    """Append one line to voicetype.log. Best-effort: never raise."""
    try:
        SUPPORT.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:  # noqa: BLE001
        pass
