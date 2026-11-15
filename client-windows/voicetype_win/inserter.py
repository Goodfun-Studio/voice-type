"""Delivering text: clipboard + simulated Ctrl+V, and the daily history file.

Mirrors the macOS ``deliver``: put the text on the clipboard, make sure the
window that was frontmost when recording started is frontmost again, then send
Ctrl+V. The clipboard-first order also means a failed paste still leaves the text
recoverable with a manual Ctrl+V.
"""
from __future__ import annotations

import time

from . import winsys
from .config import HISTORY_DIR, log_line


def write_history(text: str) -> None:
    """Append `` - HH:MM:SS  text`` to history/YYYY-MM-DD.md (best effort)."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        clock = time.strftime("%H:%M:%S")
        with open(HISTORY_DIR / f"{day}.md", "a", encoding="utf-8") as handle:
            handle.write(f"- {clock}  {text}\n")
    except Exception as exc:  # noqa: BLE001
        log_line(f"history write failed: {exc}")


def insert_text(text: str, target_hwnd: int | None = None) -> bool:
    """Copy ``text`` and paste it into the focused window. Returns True if the
    keystroke was dispatched (not a guarantee the app accepted it)."""
    if not winsys.HAS_WIN32:
        log_line("insert_text: not on Windows, text left in memory only")
        return False

    copy = winsys.set_clipboard_text(text)
    if not copy:
        log_line("clipboard set failed; still attempting paste")

    if target_hwnd:
        try:
            current = winsys.get_foreground_window()
            if current != target_hwnd:
                winsys.set_foreground_window(target_hwnd)
                # Give the window manager a moment to actually transfer focus
                # before the keystroke, else the paste lands on nothing.
                time.sleep(0.14)
        except Exception as exc:  # noqa: BLE001
            log_line(f"refocus target failed: {exc}")

    try:
        ok = winsys.send_ctrl_v()
    except Exception as exc:  # noqa: BLE001
        log_line(f"SendInput Ctrl+V failed: {exc}")
        return False
    if not ok:
        log_line("SendInput returned fewer events than sent; paste may not land")
    return ok
