"""
ui/splash.py — Startup splash screen.

An undecorated, theme-matched window shown while the main app comes up:
an animated starfield background behind a typewriter animation of the app
name with a blinking cursor. Sized and positioned identically to the main
window (see ui/geometry.py) so the handoff from splash to app is seamless
— no resize or repositioning jump.

Runs BEFORE the main window exists, as its own tiny plain-tkinter mainloop
(customtkinter widgets aren't needed here — it's a Canvas and some text).
Always shows for a fixed ~3 seconds, then tears itself down; app/main.py
then builds the real MainWindow. Sequential, single-mainloop-at-a-time —
the simplest reliable way to do a splash with Tk, and it means the splash
never fights the main window for the event loop.

Must be called AFTER apply_theme(config.theme) so TOKENS reflect the
user's chosen theme — the splash's colors are read straight from TOKENS.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

from ui.geometry import resolve_geometry, get_physical_screen_size
from ui.theme import TOKENS
from ui.widgets.starfield import Starfield
from ui.win_effects import round_window_corners

_TITLE = "Darko's Windows Utility Tool"
_TOTAL_MS = 3000          # splash always shows for this long, regardless of load speed
_CHAR_INTERVAL_MS = 45    # typing speed
_CURSOR_BLINK_MS = 420


def show_splash() -> None:
    """Blocking — runs its own mainloop and returns once the splash closes."""
    root = tk.Tk()
    root.overrideredirect(True)   # no title bar / borders — pure splash look
    round_window_corners(root)
    root.configure(bg=TOKENS["bg_shell"])

    # Same size + position the main window will open at (see ui/geometry.py)
    # so there's no visible jump when the splash hands off to the real app.
    root.update_idletasks()
    screen_w, screen_h = get_physical_screen_size()
    if screen_w <= 0 or screen_h <= 0:
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
    x, y, w, h = resolve_geometry(screen_w, screen_h)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.update()   # force Tk to actually process/apply the geometry now, synchronously
    # Same defensive re-apply as the main window: an override-redirect
    # window's initial geometry can get silently reset once it actually
    # gets mapped to the screen (no window-manager negotiation happens
    # for these, so nothing guarantees the first request "sticks"). The
    # main window already re-applies at 30/150/400ms for exactly this
    # reason — the splash never did, which is a real gap, not a guess.
    root.after(20, lambda: root.geometry(f"{w}x{h}+{x}+{y}"))
    root.after(100, lambda: root.geometry(f"{w}x{h}+{x}+{y}"))
    root.after(300, lambda: root.geometry(f"{w}x{h}+{x}+{y}"))
    try:
        from core.logger import get_logger
        get_logger(__name__).info(
            "Splash geometry: screen=%sx%s -> %sx%s+%s+%s",
            screen_w, screen_h, w, h, x, y,
        )
    except Exception:
        pass

    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    field = Starfield(
        root, bg=TOKENS["bg_shell"], star_color=TOKENS["accent"],
        max_stars=140, highlightthickness=1,
        highlightbackground=TOKENS["border"], highlightcolor=TOKENS["accent"],
    )
    field.place(relx=0, rely=0, relwidth=1, relheight=1)

    text_var = tk.StringVar(master=root, value="")
    font_size = max(16, min(26, w // 40))
    type_font = tkfont.Font(root=root, family="Consolas", size=font_size, weight="bold")
    # Pin the label's LEFT edge at a fixed x so text grows left-to-right
    # with the cursor trailing at the right, instead of re-centering (and
    # visibly jumping) on every keystroke. The fixed x is chosen so the
    # finished string ends up centered in the window.
    full_width = type_font.measure(_TITLE + "|")
    start_x = max((w - full_width) // 2, 12)
    label = tk.Label(
        root, textvariable=text_var,
        font=type_font, anchor="w", justify="left",
        fg=TOKENS["text_primary"], bg=TOKENS["bg_shell"],
    )
    label.place(x=start_x, rely=0.46, anchor="w")

    sub = tk.Label(
        root, text="DWUT",
        font=("Consolas", max(9, font_size - 7)),
        fg=TOKENS["text_disabled"], bg=TOKENS["bg_shell"],
    )
    sub.place(relx=0.5, rely=0.62, anchor="center")

    field.start()

    state = {"i": 0, "cursor_on": True, "closed": False, "after_id": None}

    def _type_next() -> None:
        if state["closed"]:
            return
        if state["i"] <= len(_TITLE):
            shown = _TITLE[: state["i"]]
            cursor = "|" if state["cursor_on"] else " "
            text_var.set(f"{shown}{cursor}")
            state["i"] += 1
            state["after_id"] = root.after(_CHAR_INTERVAL_MS, _type_next)
        else:
            _blink_cursor()

    def _blink_cursor() -> None:
        if state["closed"]:
            return
        state["cursor_on"] = not state["cursor_on"]
        cursor = "|" if state["cursor_on"] else " "
        text_var.set(f"{_TITLE}{cursor}")
        state["after_id"] = root.after(_CURSOR_BLINK_MS, _blink_cursor)

    def _finish() -> None:
        if state["closed"]:
            return
        state["closed"] = True
        # Cancel whichever typing/blink callback is currently pending —
        # the state["closed"] check above only helps if the Python callback
        # itself gets to run; a call already sitting in Tcl's event queue
        # when destroy() runs fails at the Tcl level ("invalid command
        # name") before Python ever sees it, since destroy() invalidates
        # the interpreter's command table out from under it. Cancelling
        # explicitly avoids that entirely.
        if state["after_id"] is not None:
            try:
                root.after_cancel(state["after_id"])
            except Exception:
                pass
        field.stop()
        try:
            root.destroy()
        except Exception:
            pass

    state["after_id"] = root.after(150, _type_next)   # small delay before typing starts
    root.after(_TOTAL_MS, _finish)                      # hard 3-second ceiling, per spec

    try:
        root.mainloop()
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
