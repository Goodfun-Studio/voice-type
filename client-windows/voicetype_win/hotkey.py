"""Global hotkey via a WH_KEYBOARD_LL low-level keyboard hook.

Deliberately *not* the ``keyboard`` library: that installs a filter driver and
gets blocked by most endpoint security products. A low-level hook needs no
driver and no admin rights.

Rules that keep the hook healthy (Windows silently unhooks a proc that takes too
long, or that stops returning in time):

* the callback does only O(1) work — read a couple of flags, push one tuple onto
  a queue — then returns ``CallNextHookEx`` immediately;
* it swallows **nothing** unless the Esc key is pressed while a take is live
  (see ``is_active``). Every other key is passed through untouched;
* the actual work (start/stop recording, cancel) runs on a worker thread that
  drains the queue.

Detection matches the macOS FnHotkey: two presses of the same key within 450 ms.
On Windows the key is the *right* Ctrl (VK_RCONTROL = 0xA3).
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes

from . import winsys
from .config import log_line

DOUBLE_TAP_WINDOW = 0.45      # seconds, same as macOS
MAX_HOLD = 0.5                # a "tap" longer than this doesn't count as a tap


class LowLevelKeyboardHook:
    """Global right-Ctrl double-tap detector + recording-scoped Esc cancel.

    ``is_active`` is polled *inside* the hook callback, so it must be cheap and
    thread-safe (a locked boolean read is fine). Esc is swallowed only while it
    returns True.
    """

    def __init__(self, on_double_ctrl, is_active, on_escape):
        self.on_double_ctrl = on_double_ctrl
        self._is_active = is_active
        self.on_escape = on_escape

        self._queue: "queue.Queue[tuple[str, float]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._hook = None            # HHOOK
        self._thread_id = 0
        self._proc_ref = None        # keep the CFUNCTYPE alive
        self._running = False
        # accessed only from the hook callback (single-threaded by OS contract)
        self._ctrl_down = False
        # accessed only from the worker
        self._last_tap = 0.0
        self._pressed_at = 0.0
        self._esc_latched = False

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not winsys.HAS_WIN32:
            raise winsys.WindowsUnavailable(
                f"low-level keyboard hook needs Windows ({winsys.IMPORT_ERROR})")
        if self._thread is not None:
            return
        self._running = True
        # LowLevelKeyboardProc = LRESULT CALLBACK (int nCode, WPARAM, LPARAM)
        self._proc_ref = ctypes.WINFUNCTYPE(
            winsys.LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
        )(self._callback)
        self._thread = threading.Thread(target=self._thread_main, name="vtw-hook", daemon=True)
        self._thread.start()
        self._worker = threading.Thread(target=self._worker_main, name="vtw-hook-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._thread_id and winsys.HAS_WIN32:
            try:
                winsys.user32.PostThreadMessageW(self._thread_id, winsys.WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        self._queue.put(("stop", time.monotonic()))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ---- hook plumbing ------------------------------------------------------
    def _thread_main(self) -> None:
        self._thread_id = winsys.kernel32.GetCurrentThreadId()
        module = winsys.kernel32.GetModuleHandleW(None)
        self._hook = winsys.user32.SetWindowsHookExW(
            winsys.WH_KEYBOARD_LL, self._proc_ref, module, 0
        )
        if not self._hook:
            err = ctypes.get_last_error()
            log_line(f"SetWindowsHookExW failed (err={err}); hotkey disabled")
            return
        log_line("low-level keyboard hook installed (double right-Ctrl)")

        msg = wintypes.MSG()
        while True:
            ret = winsys.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            winsys.user32.TranslateMessage(ctypes.byref(msg))
            winsys.user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            winsys.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        log_line("keyboard hook removed")

    def _callback(self, n_code, w_param, l_param):
        # Fast path: pass everything through unless it is ours.
        if n_code == winsys.HC_ACTION:
            try:
                kb = ctypes.cast(l_param, ctypes.POINTER(winsys.KBDLLHOOKSTRUCT)).contents
                if not (kb.flags & winsys.LLKHF_INJECTED):
                    vk = kb.vkCode
                    down = w_param in (winsys.WM_KEYDOWN, winsys.WM_SYSKEYDOWN)
                    up = w_param in (winsys.WM_KEYUP, winsys.WM_SYSKEYUP)

                    if vk == winsys.VK_ESCAPE and self._is_active():
                        # Swallow down *and* up for the whole take so the app
                        # underneath never sees a stray Esc. Fire cancel once.
                        if down and not self._esc_latched:
                            self._esc_latched = True
                            self._queue.put(("escape", time.monotonic()))
                        if up:
                            self._esc_latched = False
                        return 1  # consumed

                    if vk == winsys.VK_RCONTROL:
                        if down and not self._ctrl_down:
                            self._ctrl_down = True
                            self._queue.put(("ctrl_down", time.monotonic()))
                        elif up:
                            self._ctrl_down = False
            except Exception:  # noqa: BLE001
                pass
        return winsys.user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    # ---- worker -------------------------------------------------------------
    def _worker_main(self) -> None:
        while self._running:
            try:
                kind, when = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "stop":
                break
            if kind == "escape":
                self._esc_latched = False
                try:
                    self.on_escape()
                except Exception as exc:  # noqa: BLE001
                    log_line(f"escape handler error: {exc}")
            elif kind == "ctrl_down":
                if when - self._pressed_at > MAX_HOLD:
                    self._last_tap = 0.0
                self._pressed_at = when
                if self._last_tap and (when - self._last_tap) <= DOUBLE_TAP_WINDOW:
                    self._last_tap = 0.0
                    try:
                        self.on_double_ctrl()
                    except Exception as exc:  # noqa: BLE001
                        log_line(f"double-ctrl handler error: {exc}")
                else:
                    self._last_tap = when
