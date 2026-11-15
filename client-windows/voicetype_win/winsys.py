"""Thin, guarded ctypes layer over the Win32 bits we need.

Everything here degrades to a clear error off-Windows so the package still
imports (and py_compiles) on the Mac. The two things the rest of the code leans
on and that are easy to get wrong:

* ``apply_no_activate`` — WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW on the HUD so it
  never steals focus from the window you are typing into.
* ``send_ctrl_v`` — a correctly laid out INPUT array for SendInput. The struct
  alignment on 64-bit is the classic footgun (INPUT is 40 bytes there); we let
  ctypes do natural alignment rather than hand-packing.
"""
from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

# --- value types -------------------------------------------------------------
# ULONG_PTR / LONG_PTR: pointer-sized. c_size_t is exactly that.
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

# --- constants ---------------------------------------------------------------
WH_KEYBOARD_LL = 13
HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_RCONTROL = 0xA3
VK_V = 0x56

LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

SW_RESTORE = 9


class WindowsUnavailable(RuntimeError):
    """Raised when a win32 call is attempted off-Windows or after a bind error."""


# --- structures (safe to define on any platform) -----------------------------
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUTUNION),
    ]


# --- library binding ---------------------------------------------------------
HAS_WIN32 = False
IMPORT_ERROR: str | None = None

if IS_WINDOWS:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        HAS_WIN32 = True
    except Exception as exc:  # noqa: BLE001
        user32 = kernel32 = shell32 = None  # type: ignore[assignment]
        IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    user32 = kernel32 = shell32 = None  # type: ignore[assignment]
    IMPORT_ERROR = "not running on Windows"

if HAS_WIN32:
    # GetLongPathName-style pointer-width API names differ between 32/64-bit.
    _GetWindowLong = getattr(user32, "GetWindowLongPtrW",
                             getattr(user32, "GetWindowLongW", None))
    _SetWindowLong = getattr(user32, "SetWindowLongPtrW",
                             getattr(user32, "SetWindowLongW", None))

    # ---- prototype the calls we use (avoids silent truncation of handles) ----
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetForegroundWindow.argtypes = []

    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_bool

    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_bool

    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_bool

    user32.GetParent.argtypes = [ctypes.c_void_p]
    user32.GetParent.restype = ctypes.c_void_p

    if _GetWindowLong is not None:
        _GetWindowLong.restype = ctypes.c_ssize_t
        _GetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int]
    if _SetWindowLong is not None:
        _SetWindowLong.restype = ctypes.c_ssize_t
        _SetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_ulong]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    user32.UnhookWindowsHookEx.restype = ctypes.c_bool
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.PostThreadMessageW.restype = ctypes.c_bool
    user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_ssize_t]

    user32.SendInput.restype = ctypes.c_uint
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []

    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    shell32.IsUserAnAdmin.restype = ctypes.c_bool
    shell32.IsUserAnAdmin.argtypes = []


def _require() -> None:
    if not HAS_WIN32:
        raise WindowsUnavailable(
            f"Windows API unavailable ({IMPORT_ERROR}). "
            "This client only runs on Windows."
        )


# --- focus / foreground ------------------------------------------------------
def get_foreground_window() -> int:
    _require()
    return int(user32.GetForegroundWindow() or 0)


def set_foreground_window(hwnd: int) -> bool:
    """Best-effort. Windows may refuse to foreground a window that isn't the
    caller's; SW_RESTORE first helps when the target is minimised."""
    _require()
    if not hwnd:
        return False
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
    except Exception:  # noqa: BLE001
        pass
    return bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))


def is_admin() -> bool:
    if not HAS_WIN32:
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# --- no-activate extended window style --------------------------------------
def apply_no_activate(hwnd: int) -> tuple[int, int] | None:
    """Set WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW on a window. Returns
    (old, new) ex-style on success, None on failure. This is what keeps the HUD
    from pulling focus away from the app you're pasting into."""
    _require()
    if not hwnd or _GetWindowLong is None or _SetWindowLong is None:
        return None
    ex = int(_GetWindowLong(ctypes.c_void_p(hwnd), GWL_EXSTYLE))
    new = ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    _SetWindowLong(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new)
    return ex, int(_GetWindowLong(ctypes.c_void_p(hwnd), GWL_EXSTYLE))


# --- clipboard ---------------------------------------------------------------
def set_clipboard_text(text: str, retries: int = 8) -> bool:
    """Put ``text`` on the clipboard as CF_UNICODETEXT. Another process can hold
    the clipboard open for a moment, so retry briefly before giving up."""
    _require()
    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return False
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)

    import time

    for _ in range(max(1, retries)):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                # Ownership of `handle` transfers to the clipboard on success.
                if not user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle)):
                    return False
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    return False


# --- SendInput ---------------------------------------------------------------
def _key_event(vk: int, *, up: bool = False, extended: bool = False) -> INPUT:
    flags = 0
    if up:
        flags |= KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def send_input(events: list[INPUT]) -> int:
    _require()
    if not events:
        return 0
    array = (INPUT * len(events))(*events)
    return int(user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(INPUT)))


def send_ctrl_v() -> bool:
    """Simulate Ctrl+V. Right Ctrl is deliberately *not* released here — we only
    press the left Ctrl — so a still-held right Ctrl from the hotkey can't fight
    us."""
    events = [
        _key_event(VK_CONTROL),
        _key_event(VK_V),
        _key_event(VK_V, up=True),
        _key_event(VK_CONTROL, up=True),
    ]
    sent = send_input(events)
    return sent == len(events)


def input_struct_size() -> int:
    """Exposed for --selftest: 40 on 64-bit, 28 on 32-bit."""
    return ctypes.sizeof(INPUT)
