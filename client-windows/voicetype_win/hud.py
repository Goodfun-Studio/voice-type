"""Floating level meter: a rounded, translucent, always-on-top capsule at the
bottom-centre of the screen — the Windows twin of the macOS HUD.

The single most important line in this file is the WS_EX_NOACTIVATE style. A
topmost Tk window steals focus the instant it is mapped, which would make the
Ctrl+V in ``inserter`` land on the *HUD* instead of the document you were
typing in. ``overrideredirect`` + WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW keeps it
painted but non-activating.

Threading: Tk may only be touched from the thread that created it. Public
methods are therefore called from the hotkey/recorder threads and just enqueue
commands; the main thread drains them in ``_pump``.
"""
from __future__ import annotations

import queue
import time

try:
    import tkinter as tk
except Exception as _exc:  # noqa: BLE001
    tk = None  # type: ignore[assignment]
    _TK_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _TK_ERROR = None

from . import winsys
from .config import log_line

WIDTH = 300
HEIGHT = 54
ALPHA = 0.88
SUPPORTED_BARS = 26
PUMP_MS = 16  # ~60 fps


def _round_rect(canvas, x1, y1, x2, y2, r, **kwargs):  # noqa: ANN001
    """A rounded rectangle, drawn as a smoothed polygon (Tk has no native one)."""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class HUD:
    """Owns a hidden Tk root. Create on the main thread; then call run()."""

    def __init__(self):
        if tk is None:
            raise RuntimeError(f"tkinter unavailable ({_TK_ERROR})")
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._root = tk.Tk()
        self._root.withdraw()
        self._panel = tk.Toplevel(self._root)
        self._panel.overrideredirect(True)
        self._panel.attributes("-topmost", True)
        try:
            self._panel.attributes("-alpha", ALPHA)
        except Exception:  # noqa: BLE001
            pass
        self._canvas = tk.Canvas(self._panel, width=WIDTH, height=HEIGHT,
                                 highlightthickness=0, bd=0, bg="#000000")
        self._canvas.pack()

        self._mode = "idle"            # idle | listening | message
        self._message = ""
        self._levels: list[float] = []
        self._last_level_at = 0.0
        self._dismiss_at: float | None = None
        self._visible = False

        self._apply_no_activate()

    # ---- win32 tweaks -------------------------------------------------------
    def _hwnd_candidates(self) -> list[int]:
        """The Tk toplevel HWND may be ``winfo_id()`` itself (overrideredirect)
        or one level up (a managed wrapper). We don't know which on a given Tk
        build, so apply the style to both; styling a hidden owner window is
        harmless, whereas missing the real one means the HUD steals focus."""
        out: list[int] = []
        try:
            hwnd = int(self._panel.winfo_id())
        except Exception:  # noqa: BLE001
            return out
        if hwnd:
            out.append(hwnd)
        if winsys.HAS_WIN32 and hwnd and hasattr(winsys.user32, "GetParent"):
            try:
                parent = int(winsys.user32.GetParent(hwnd) or 0)
                if parent and parent not in out:
                    out.append(parent)
            except Exception:  # noqa: BLE001
                pass
        return out

    def _apply_no_activate(self) -> None:
        if not winsys.HAS_WIN32:
            return
        candidates = self._hwnd_candidates()
        if not candidates:
            log_line("HUD: no HWND yet; WS_EX_NOACTIVATE not applied")
            return
        for hwnd in candidates:
            try:
                result = winsys.apply_no_activate(hwnd)
                log_line(f"HUD: WS_EX_NOACTIVATE applied to hwnd={hwnd} -> {result}")
            except Exception as exc:  # noqa: BLE001
                log_line(f"HUD: apply_no_activate(hwnd={hwnd}) failed: {exc}")

    # ---- public, thread-safe API -------------------------------------------
    def show_listening(self) -> None:
        self._q.put(("listening",))

    def push_level(self, rms: float) -> None:
        self._q.put(("level", float(rms)))

    def show_transcribing(self) -> None:
        self._q.put(("message", "转写中…", None))

    def toast(self, text: str, seconds: float = 1.5) -> None:
        self._q.put(("message", text, seconds))

    def hide(self) -> None:
        self._q.put(("hide",))

    # ---- main-thread loop ---------------------------------------------------
    def run(self) -> None:
        """Enter the Tk main loop. Returns when the window is destroyed."""
        self._apply_no_activate()  # retry once the window is really mapped
        self._root.after(PUMP_MS, self._pump)
        self._root.mainloop()

    def destroy(self) -> None:
        self._q.put(("quit",))

    def _pump(self) -> None:
        try:
            while True:
                cmd = self._q.get_nowait()
                self._handle(cmd)
        except queue.Empty:
            pass

        if self._mode == "listening":
            if self._levels and (time.monotonic() - self._last_level_at) > 0.08:
                self._levels = [v * 0.8 for v in self._levels]
            self._draw_bars()

        if self._dismiss_at is not None and time.monotonic() >= self._dismiss_at:
            self._dismiss_at = None
            self._hide_now()

        self._root.after(PUMP_MS, self._pump)

    def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "level":
            rms = cmd[1]
            self._last_level_at = time.monotonic()
            shaped = min(1.0, pow(max(0.0, rms) * 14.0, 0.65))
            self._levels.append(shaped)
            if len(self._levels) > SUPPORTED_BARS:
                del self._levels[: len(self._levels) - SUPPORTED_BARS]
        elif kind == "listening":
            self._dismiss_at = None
            self._mode = "listening"
            self._levels = []
            self._show()
        elif kind == "message":
            self._dismiss_at = None
            self._mode = "message"
            self._message = cmd[1]
            seconds = cmd[2]
            self._show()
            if seconds is not None:
                self._dismiss_at = time.monotonic() + float(seconds)
        elif kind == "hide":
            self._dismiss_at = None
            self._hide_now()
        elif kind == "quit":
            try:
                self._root.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ---- drawing ------------------------------------------------------------
    def _place(self) -> None:
        try:
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = int((sw - WIDTH) / 2)
            y = int(sh - HEIGHT - 96)
            self._panel.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    def _show(self) -> None:
        self._place()
        self._draw_base()
        if self._mode == "message":
            self._draw_message()
        else:
            self._draw_bars()
        # Set the non-activating style *before* the window is mapped, so the
        # very first paint can't pull focus (and the paste can't land on us).
        self._apply_no_activate()
        self._panel.deiconify()
        self._panel.lift()
        try:
            self._panel.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        self._visible = True

    def _hide_now(self) -> None:
        if self._visible:
            self._panel.withdraw()
            self._visible = False

    def _draw_base(self) -> None:
        self._canvas.delete("all")
        _round_rect(self._canvas, 1, 1, WIDTH - 1, HEIGHT - 1, HEIGHT / 2,
                    fill="#111111", outline="#3a3a3a", width=1)
        if self._mode == "listening":
            self._draw_mic()
            self._canvas.create_text(WIDTH - 14, HEIGHT / 2, text="esc 取消",
                                     anchor="e", fill="#6b6b6b",
                                     font=("Microsoft YaHei UI", 9))

    def _draw_mic(self) -> None:
        x, mid = 22.0, HEIGHT / 2
        # mic body + stand, a cheap stand-in for the SF Symbol the Mac uses.
        _round_rect(self._canvas, x - 4, mid - 10, x + 4, mid + 2, 4, fill="#d9d9d9")
        self._canvas.create_line(x, mid + 2, x, mid + 8, fill="#d9d9d9", width=2)
        self._canvas.create_line(x - 6, mid + 8, x + 6, mid + 8, fill="#d9d9d9", width=2)

    def _draw_bars(self) -> None:
        self._canvas.delete("bars")
        start_x = 42.0
        hint_w = 60.0
        bar_w, gap = 3.0, 3.0
        available = (WIDTH - 14 - hint_w) - start_x - 12
        max_bars = max(1, int((available + gap) / (bar_w + gap)))
        shown = self._levels[-max_bars:]
        mid = HEIGHT / 2
        max_h = HEIGHT - 22
        total = len(shown) * (bar_w + gap) - gap if shown else 0
        x = start_x + (available - total) / 2
        for index, level in enumerate(shown):
            height = max(3.0, level * max_h)
            latest = index == len(shown) - 1
            color = "#6bc7ff" if latest else "#8c8c8c"
            self._canvas.create_line(x, mid - height / 2, x, mid + height / 2,
                                     fill=color, width=bar_w, capstyle="round",
                                     tags="bars")
            x += bar_w + gap

    def _draw_message(self) -> None:
        self._canvas.delete("msg")
        text = self._message
        if len(text) > 28:
            text = text[:28] + "…"
        self._canvas.create_text(WIDTH / 2, HEIGHT / 2, text=text, fill="#ffffff",
                                 font=("Microsoft YaHei UI", 12), tags="msg")
