"""Microphone capture straight into a 16 kHz mono 16-bit WAV.

That is exactly what whisper wants, so there is no resample step on the way out
— matching the macOS Recorder. Recording goes through sounddevice (PortAudio,
WASAPI on Windows); RMS is computed per block and handed to the HUD.
"""
from __future__ import annotations

import tempfile
import threading
import time
import wave
from pathlib import Path

try:  # optional so the package imports on a machine without the audio stack
    import numpy as np
except Exception as _exc:  # noqa: BLE001
    np = None  # type: ignore[assignment]
    _NUMPY_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _NUMPY_ERROR = None

try:
    import sounddevice as sd
except Exception as _exc:  # noqa: BLE001
    sd = None  # type: ignore[assignment]
    _SD_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _SD_ERROR = None

TARGET_RATE = 16_000
TARGET_CHANNELS = 1
BLOCK = 1024


class RecorderError(RuntimeError):
    pass


class Recorder:
    """start() opens the stream; stop() returns the finished WAV path (or None)."""

    def __init__(self, samplerate: int = TARGET_RATE, channels: int = TARGET_CHANNELS):
        self.samplerate = samplerate
        self.channels = channels
        self.on_level = None                 # callable(float rms) or None
        self._stream = None
        self._chunks: list[bytes] = []
        self._frames = 0
        self._lock = threading.Lock()
        self._url: Path | None = None
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        # Runs on PortAudio's thread — keep it tight, never raise.
        try:
            with self._lock:
                self._chunks.append(indata.tobytes())
                self._frames += frames
            if self.on_level is not None and np is not None and len(indata) > 0:
                block = indata[:, 0].astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(block * block)))
                self.on_level(rms)
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> Path:
        if sd is None:
            raise RecorderError(f"sounddevice unavailable ({_SD_ERROR})")
        if self._recording:
            raise RecorderError("already recording")

        self._chunks = []
        self._frames = 0
        self._url = Path(tempfile.gettempdir()) / f"voicetype-{int(time.time() * 1000)}.wav"

        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                blocksize=BLOCK,
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise RecorderError(str(exc)) from exc

        self._stream = stream
        self._recording = True
        return self._url

    def stop(self) -> Path | None:
        if not self._recording:
            return None
        self._recording = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        with self._lock:
            payload = b"".join(self._chunks)
            self._chunks = []
            frames = self._frames

        url = self._url
        self._url = None
        if url is None:
            return None
        if frames <= 0 or not payload:
            return None

        with wave.open(str(url), "wb") as handle:
            handle.setnchannels(self.channels)
            handle.setsampwidth(2)
            handle.setframerate(self.samplerate)
            handle.writeframes(payload)
        return url

    @property
    def seconds(self) -> float:
        with self._lock:
            return self._frames / float(self.samplerate)
