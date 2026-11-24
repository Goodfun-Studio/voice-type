"""Transcribe a WAV that is still being recorded.

⚠️ 已停用（2026-11-24）—— app 侧不再调用本模块。

停用原因（boss 实测一周的真实数据，不是理论推演）：
  · 尾延迟反而退化成 20-35 秒（设计目标是 1.2 秒）：录音期间要一边转写一边
    跟随文件，机器一忙（他会同时跑 Claude Code 等）就追不上，积压全部堆到收尾
  · 输出碎片化："好。好。挥直。先按照你的建议回在消息线程里看看效果。" /
    "爆400的人。把4600解了。" —— 短段落丢上下文，质量明显低于一次性整段
  · 我当初的基准测试是在**空载机器 + 干净 TTS 素材**上做的（CER 0.0235 vs 0.0201），
    那个条件不代表真实使用 —— 这是本次判断失误的根源

结论：本地 whisper 的固定 30 秒窗口成本，换不来"边说边出字"的收益；
一次性整段在真实环境下又快又准。代码保留供日后评估，但**默认不要打开**。


The app writes its recording to a file as it goes; this polls that file, cuts it
at *silence* boundaries, and transcribes each finished piece while the user is
still talking. When the user stops, only the tail is left to transcribe — so the
wait after stopping is roughly constant instead of growing with sentence length.

Cutting on silence (rather than every N seconds) matters: a cut inside a word or
phrase loses the context whisper needs for punctuation and can drop syllables at
the seam. A pause is the only place a cut is free.

Design notes:
- Runs in its own thread so a state query never blocks on the model.
- The model is shared with the rest of the engine, so all inference goes through
  the engine's model lock.
- Text is accumulated and merged at the end; no per-segment cleanup is applied
  until then, because cleanup rules (vocabulary, punctuation) work better on the
  whole text.
"""
from __future__ import annotations

import os
import struct
import threading
import time
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
FRAME_MS = 30                      # RMS window
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
SILENCE_FRAMES_TO_CUT = 8          # ~240ms of quiet ends a segment
MIN_SEGMENT_SECONDS = 1.2

TERMINAL_PUNCT = "。！？…!?."
# 一段没以句末标点结尾就先不提交，等下一个停顿 —— 拿到完整句子比切在句中间强。
MAX_UNCOMMITTED_SECONDS = 12.0          # do not bother with tiny fragments
MAX_SEGMENT_SECONDS = 25.0         # never let a segment approach whisper's 30s window


# 可调参数（也可用环境变量覆盖，便于做配置扫描）
SILENCE_MS = int(os.environ.get("VOICETYPE_STREAM_SILENCE_MS", "240"))
CARRY_CHARS = int(os.environ.get("VOICETYPE_STREAM_CARRY_CHARS", "24"))


def find_data_offset(path: Path) -> int:
    """Byte offset of the PCM payload. The header is still being written when we
    first look (the size field is a placeholder), so parse chunks rather than
    assuming 44 bytes."""
    with path.open("rb") as handle:
        if handle.read(4) != b"RIFF":
            return 44
        handle.seek(12)
        while True:
            header = handle.read(8)
            if len(header) < 8:
                return 44
            chunk_id, size = header[:4], struct.unpack("<I", header[4:])[0]
            if chunk_id == b"data":
                return handle.tell()
            handle.seek(size + (size & 1), 1)   # chunks are word aligned


class StreamSession:
    def __init__(self, wav_path: str, language: str = "zh",
                 transcribe=None, lock: threading.Lock | None = None,
                 context_carry: bool = True, junk_filter=None,
                 silence_ms: int | None = None, carry_chars: int | None = None,
                 sentence_align: bool | None = None) -> None:
        self.context_carry = context_carry
        self._junk_filter = junk_filter
        self.silence_frames = max(1, (silence_ms if silence_ms is not None else SILENCE_MS) // FRAME_MS)
        self.carry_chars = carry_chars if carry_chars is not None else CARRY_CHARS
        # 实测：本素材上 CER 无变化（0.0235），只让尾延迟好看一点，代价是重复转写。
        # 由于测不出质量收益，默认关闭；真实口语素材（停顿不在句末）值得再评一次。
        self.sentence_align = (sentence_align if sentence_align is not None
                              else os.environ.get("VOICETYPE_STREAM_SENTENCE_ALIGN", "0") == "1")
        self._preview = ""                     # 未提交的预览（HUD 用，不进最终文本）
        self._all_pcm = bytearray()            # 全程 PCM，供"整段重转"用
        self.reconcile = os.environ.get("VOICETYPE_STREAM_RECONCILE", "0") == "1"
        self._last_cut_tail = bytearray()      # 上一段的尾部音频，用于下一段的左侧重叠
        self.path = Path(wav_path)
        self.language = language
        self._transcribe = transcribe          # callable(wav_path, language) -> text
        self._lock = lock or threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._offset = 0
        self._pending = bytearray()            # bytes read from disk, not yet framed
        self._carry = bytearray()              # framed audio of the segment in progress
        self._segments: list[str] = []
        self._context = ""                     # tail of the previous segment, fed forward
        self._quiet_run = 0
        self._loud_rms: list[float] = []       # to adapt the silence threshold
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.segment_seconds: list[float] = []

    # ---- public API -------------------------------------------------------

    def start(self) -> None:
        self._offset = find_data_offset(self.path) if self.path.exists() else 0
        self._thread = threading.Thread(target=self._loop, name="vt-stream", daemon=True)
        self._thread.start()

    def state(self) -> dict:
        return {
            "partial": "".join(self._segments) + self._preview,
            "segments": len(self._segments),
            "audioSeconds": round(len(self._pending) / 2 / SAMPLE_RATE + sum(self.segment_seconds), 2),
            "elapsedMs": int((time.time() - self.started_at) * 1000),
        }

    def finish(self, timeout: float = 30.0) -> dict:
        """Stop tailing, transcribe whatever is left, return the merged text."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        started = time.time()
        # Read once more: audio written after the last poll is still on disk and
        # would otherwise be dropped (this cost us the final sentence in testing).
        try:
            self._read_new_audio()
        except Exception:  # noqa: BLE001
            pass
        self._flush_pending(force=True)
        merged = "".join(self._segments)
        reconciled = ""
        if self.reconcile and merged:
            try:
                reconciled = self._reconcile()
            except Exception as exc:  # noqa: BLE001
                try:
                    from engine import log as _log
                    _log(f"reconcile failed: {exc}")
                except Exception:  # noqa: BLE001
                    pass
        self.finished_at = time.time()
        if reconciled:
            # 整段重转的结果优先，但照样过一遍护栏（退回合并文本时也是）
            return {
                "text": reconciled, "reconciled": True,
                "segments": len(self._segments),
                "tailMs": int((self.finished_at - started) * 1000),
            }
        return {
            "text": merged,
            "segments": len(self._segments),
            "tailMs": int((self.finished_at - started) * 1000),
        }

    # ---- internals --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_new_audio()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)

    def _read_new_audio(self) -> None:
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        if not chunk:
            return
        self._offset += len(chunk)
        self._pending.extend(chunk)

        # Walk whole frames, tracking where speech stops.
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            rms = _rms(frame)
            if rms >= self._silence_threshold():
                self._quiet_run = 0
                self._loud_rms.append(rms)
                if len(self._loud_rms) > 400:
                    del self._loud_rms[:200]
            else:
                self._quiet_run += 1
            self._carry.extend(frame)
            self._all_pcm.extend(frame)

            seconds = len(self._carry) / 2 / SAMPLE_RATE
            if self._quiet_run >= self.silence_frames and seconds >= MIN_SEGMENT_SECONDS:
                self._cut(self._carry)
            elif seconds >= MAX_SEGMENT_SECONDS:
                self._cut(self._carry, force=True)

    def _cut(self, audio: bytearray, force: bool = False) -> None:
        seconds = len(audio) / 2 / SAMPLE_RATE
        # 实测记录（2026-11-17）：左侧重叠会让 CER 从 0.0235 变差到 0.0470
        # —— 上一段的尾巴（可能是静音或半个字）会让模型开头就乱。已撤除，别再试。
        segment = bytes(audio)
        audio.clear()
        self._quiet_run = 0
        if seconds < MIN_SEGMENT_SECONDS:
            return
        self.segment_seconds.append(seconds)
        text = self._run_model(segment)
        # 逐段过滤：复读/幻觉/复读提示词都是解码失败，既不该进实时预览，
        # 也不该进最终文本。判定逻辑由调用方给（引擎侧统一）。
        if text and self._junk_filter and self._junk_filter(text, _rms(segment)):
            return
        # 句末对齐：没到句子结尾就先不提交，把音频留着等下一个停顿。
        # 切在句中间会丢上下文（标点坏、词可能被切开），代价只是多转写一次。
        if (text and self.sentence_align and not force
                and not text.endswith(tuple(TERMINAL_PUNCT))
                and seconds < MAX_UNCOMMITTED_SECONDS):
            self._preview = text
            self._quiet_run = 0
            return                                  # 注意：不清空 audio，留着
        if text:
            self._preview = ""
            self._segments.append(text)
            # Carrying a little of the previous text helps the model keep the
            # punctuation and the thread of the sentence across the cut. Kept
            # short on purpose: long prompts are what causes repeat loops.
            self._context = text[-self.carry_chars:]

    def _reconcile(self) -> str:
        """质量优先：把整段音频一次性重转，取它当最终文本。

        流式的切段本身会损失一点质量（实测 CER 0.0235 vs 0.0201）。
        这个方法用「多等几秒」把差距买回来，代价随录音时长线性增长。
        """
        if not self._all_pcm:
            return ""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        with wave.open(path, "wb") as handle:      # 和 _run_model 用同一套写法
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(bytes(self._all_pcm))
        try:
            return self._transcribe(path, self.language, "") or ""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _flush_pending(self, force: bool = False) -> None:
        """Transcribe the leftover tail (and any audio still in the read buffer)."""
        if len(self._pending) > 0:
            self._carry.extend(self._pending)
            self._pending.clear()
        if self._carry:
            self._cut(self._carry, force=True)

    def _run_model(self, pcm: bytes) -> str:
        tmp = self.path.with_suffix(f".seg{len(self._segments)}.wav")
        with wave.open(str(tmp), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm)
        try:
            with self._lock:
                carry = self._context if self.context_carry else ""
                return self._transcribe(str(tmp), self.language, carry) or ""
        finally:
            tmp.unlink(missing_ok=True)

    def _silence_threshold(self) -> float:
        if not self._loud_rms:
            return 0.008
        ordered = sorted(self._loud_rms)
        p75 = ordered[int(len(ordered) * 0.75)]
        return max(0.008, p75 * 0.25)


def _rms(frame: bytes) -> float:
    import array
    samples = array.array("h")
    samples.frombytes(frame)
    if not samples:
        return 0.0
    total = sum(float(s) * float(s) for s in samples) / len(samples)
    return (total ** 0.5) / 32768.0
