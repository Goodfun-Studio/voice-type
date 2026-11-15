"""Speech-to-text backends.

One interface, two implementations:

- ``MlxBackend``     — Apple MLX. Fastest option on M-series Macs.
- ``FasterWhisper``  — CTranslate2. The cross-platform option (Windows, Linux,
                       Intel Macs), running on CPU int8 or an NVIDIA GPU.

Everything downstream — the silence gate, the hallucination filter, the cleanup
rules, the vocabulary rewrite — is backend-agnostic and shared by both.
"""
from __future__ import annotations

import os
import socket as _socket
import sys
import time
from pathlib import Path

DEFAULT_MLX_MODEL = "whisper-large-v3-turbo"


def ensure_hf_endpoint(verbose: bool = True) -> None:
    """huggingface.co is unreachable on some networks (and hard-blocked on
    others); fall back to a mirror when a 2.5s probe says so. Both backends
    download their weights through huggingface_hub, so this has to run first."""
    if os.environ.get("HF_ENDPOINT"):
        return
    try:
        probe = _socket.create_connection(("huggingface.co", 443), timeout=2.5)
        probe.close()
    except OSError:
        os.environ["HF_ENDPOINT"] = os.environ.get("VOICETYPE_HF_MIRROR", "https://hf-mirror.com")
        if verbose:
            print("huggingface.co unreachable, using", os.environ["HF_ENDPOINT"], flush=True)


class BackendError(RuntimeError):
    pass


class Backend:
    name = "base"

    def load(self, warmup_wav: Path | None = None) -> None:
        raise NotImplementedError

    @property
    def ready(self) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def transcribe(self, path: str, language: str | None, initial_prompt: str | None) -> dict:
        """Return {"text": str, "language": str | None}."""
        raise NotImplementedError


class MlxBackend(Backend):
    """Apple Silicon only. Uses the local model directory when present so a
    machine that already downloaded it never touches the network."""

    name = "mlx"

    def __init__(self, model: str | None = None) -> None:
        local = Path(__file__).resolve().parent.parent / "models" / DEFAULT_MLX_MODEL
        weights = local / "weights.safetensors"
        fallback = str(local) if weights.exists() and weights.stat().st_size > 100_000_000 \
            else f"mlx-community/{DEFAULT_MLX_MODEL}"
        self.model = model or os.environ.get("VOICETYPE_MODEL") or fallback
        self._ready = False

    def load(self, warmup_wav: Path | None = None) -> None:
        ensure_hf_endpoint()
        import mlx_whisper  # noqa: F401  (import cost is part of the warm-up)
        if warmup_wav is not None:
            # Forces Metal kernels to compile now instead of on the first
            # utterance the user actually cares about.
            mlx_whisper.transcribe(str(warmup_wav), path_or_hf_repo=self.model,
                                   language="zh", verbose=None)
        self._ready = True

    @property
    def ready(self) -> bool:
        return self._ready

    def describe(self) -> str:
        return f"mlx:{self.model}"

    def transcribe(self, path: str, language: str | None, initial_prompt: str | None) -> dict:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            path,
            path_or_hf_repo=self.model,
            language=language,
            initial_prompt=initial_prompt,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            verbose=None,
        )
        return {"text": (result.get("text") or "").strip(),
                "language": result.get("language")}


class FasterWhisperBackend(Backend):
    """CTranslate2 backend — the one Windows uses. Picks a sane default model
    for the hardware it finds: a big model only makes sense with a GPU, and on
    CPU a big model is slower than the speech it is transcribing."""

    name = "faster-whisper"

    def __init__(self, model: str | None = None, device: str | None = None,
                 compute_type: str | None = None) -> None:
        self.device = device or os.environ.get("VOICETYPE_DEVICE") or self._auto_device()
        self.compute_type = compute_type or os.environ.get("VOICETYPE_COMPUTE_TYPE") or (
            "float16" if self.device == "cuda" else "int8")
        self.model = model or os.environ.get("VOICETYPE_FW_MODEL") or (
            "large-v3-turbo" if self.device == "cuda" else "small")
        self.download_root = os.environ.get("VOICETYPE_FW_CACHE") or None
        self.beam_size = int(os.environ.get("VOICETYPE_BEAM", "5"))
        self._model = None

    @staticmethod
    def _auto_device() -> str:
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:  # noqa: BLE001
            pass
        return "cpu"

    def load(self, warmup_wav: Path | None = None) -> None:
        ensure_hf_endpoint()
        from faster_whisper import WhisperModel

        if self.download_root:
            Path(self.download_root).mkdir(parents=True, exist_ok=True)
        self._model = WhisperModel(self.model, device=self.device,
                                   compute_type=self.compute_type,
                                   download_root=self.download_root)
        if warmup_wav is not None:
            list(self._model.transcribe(str(warmup_wav), language="zh",
                                        beam_size=1, vad_filter=False)[0])

    @property
    def ready(self) -> bool:
        return self._model is not None

    def describe(self) -> str:
        return f"faster-whisper:{self.model}@{self.device}/{self.compute_type}"

    def transcribe(self, path: str, language: str | None, initial_prompt: str | None) -> dict:
        if self._model is None:
            raise BackendError("backend not loaded")
        segments, info = self._model.transcribe(
            path,
            language=language,
            initial_prompt=initial_prompt,
            beam_size=self.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            vad_filter=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        return {"text": text, "language": getattr(info, "language", None)}


def choose_backend(name: str | None = None) -> Backend:
    """Explicit name wins; otherwise use MLX on Apple Silicon (fastest there)
    and CTranslate2 everywhere else."""
    choice = (name or os.environ.get("VOICETYPE_BACKEND") or "").strip().lower()
    if choice in ("mlx", "faster-whisper", "faster_whisper"):
        return {"mlx": MlxBackend, "faster-whisper": FasterWhisperBackend,
                "faster_whisper": FasterWhisperBackend}[choice]()
    if sys.platform == "darwin" and os.uname().machine == "arm64":
        return MlxBackend()
    return FasterWhisperBackend()
