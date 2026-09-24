"""
ui/geometry.py — single source of truth for the app window's size/position.

Both the splash screen and the main window read from here, so they always
resolve to the EXACT same size and screen position — no separate "guess
where to put it" logic living in two places that can drift apart.

Position is ALWAYS a pure calculation from (screen size, window size) —
deliberately not read from any saved/mutable state. A "restore last
position" version of this previously existed, but reading a stateful
value independently from two different call sites (splash, then later
MainWindow) is exactly the kind of thing that can silently drift apart —
which is what caused the splash and main window to land in different
spots. A pure function of the same two inputs can't diverge.
"""

from __future__ import annotations

import sys

from app.config import config

# Compact "admin panel" footprint. Wider than the very first compact pass
# (860x580) — several existing toolbars (Registry Cleaner's action row,
# Debloat's action row, Optimizer's Quick Actions row) need more like
# ~650-700px of content width once the 200px sidebar is subtracted, so
# 860 was cutting it too close and could clip the last button/column on
# some of those rows. This keeps things noticeably smaller than the old
# 1020x690 default while giving those rows real breathing room, including
# down at the minimum size the user can resize to.
MIN_W = 900
MIN_H = 580
DEFAULT_W = 960
DEFAULT_H = 620


def get_physical_screen_size() -> tuple[int, int]:
    """
    Physical screen resolution via the Windows API directly — NOT via Tk's
    winfo_screenwidth()/winfo_screenheight(). Those turned out to return a
    smaller-than-real value for the splash's plain tk.Tk() root specifically
    (confirmed by screenshot: the splash consistently lands pinned to (0,0),
    while the main window — same math, same target size — lands correctly
    centered). Root cause looks like a Tk quirk querying screen size from a
    freshly-created root before it's mapped, not the centering math itself
    (which is simple and shared). GetSystemMetrics asks Windows directly,
    with nothing Tk-specific in the way, so both windows now measure the
    screen with the exact same, reliable source.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
            h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
    return 0, 0   # caller falls back to its own Tk-based query


def target_size() -> tuple[int, int]:
    """The window size to use on this launch (clamped to the minimum)."""
    w = max(int(config.window_width or DEFAULT_W), MIN_W)
    h = max(int(config.window_height or DEFAULT_H), MIN_H)
    return w, h


def resolve_geometry(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """
    Returns (x, y, w, h) for this launch — always centered on the given
    screen dimensions.

    `screen_w`/`screen_h` are a fallback only — every real caller should
    now pass get_physical_screen_size() instead of Tk's own
    winfo_screenwidth()/winfo_screenheight(), which is what actually
    caused the mismatch (see get_physical_screen_size's docstring). Kept
    as parameters so callers still work if the Windows API call fails.

    Width/height are also clamped to fit the actual screen (with a small
    margin) — a saved size from an earlier maximized/dragged-wide window
    would otherwise make the window as wide as (or wider than) the screen,
    which forces x (and/or y) to 0 and makes it LOOK pinned to a corner
    instead of centered, even though the centering math itself is correct.
    """
    w, h = target_size()

    # Never exceed the screen (minus a small margin) — this is what
    # actually guarantees a centered result regardless of what size got
    # saved from a previous session.
    if screen_w > 0:
        w = min(w, max(screen_w - 40, MIN_W))
    if screen_h > 0:
        h = min(h, max(screen_h - 40, MIN_H))

    x = max((screen_w - w) // 2, 0)
    y = max((screen_h - h) // 2, 0)
    return x, y, w, h
