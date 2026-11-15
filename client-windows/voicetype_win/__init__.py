"""voicetype_win — Windows port of the Voice Type client (macOS original: app/Sources/*.swift).

Double-tap right Ctrl to record, Esc to cancel, text lands on the clipboard and
is pasted with a simulated Ctrl+V. Talks to the same frozen engine protocol as
the macOS app (one JSON object per line over loopback TCP).

Windows-only. The package is import-safe on other platforms (all win32 calls
are guarded) so it can be syntax-checked and --selftested on macOS.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
