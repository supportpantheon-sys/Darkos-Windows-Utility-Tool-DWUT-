"""
ui/win_effects.py

Windows 11 rounds top-level window corners automatically via its own
window manager — but ONLY for normally-decorated windows. Both the splash
and the main window use overrideredirect(True) (no native titlebar), which
opts them out of that automatic rounding, leaving hard square corners.

This restores it explicitly via the DWM API (DwmSetWindowAttribute with
DWMWA_WINDOW_CORNER_PREFERENCE), which is the same mechanism Windows 11
itself uses. No-ops harmlessly on Windows 10 or older (the attribute is
simply rejected) and on any non-Windows platform.
"""

from __future__ import annotations

import sys


def round_window_corners(root) -> None:
    """Apply Windows 11's rounded-corner window style to a Tk root.
    Call after the window has an HWND (i.e. after it's been created —
    winfo_id() is valid immediately after widget construction, no need
    to wait for it to be mapped/visible)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = root.winfo_id()
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass   # Windows 10 or older, or the call just isn't available — fine either way
