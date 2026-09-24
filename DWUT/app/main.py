"""
app/main.py — DWUT entry point.

Launch order:
  1. Ensure all data directories exist
  2. Set up logging
  3. Check admin rights — if not elevated, offer to relaunch as admin (UAC)
  4. Apply theme
  5. Show loading splash (~3s, theme-matched, animated starfield)
  6. Build and run the main window (pages load lazily on first visit)
"""

from __future__ import annotations

import sys

# ── Must happen before any other dmurt imports ──────────────────────────────
from app.config import config, ensure_dirs
ensure_dirs()

from core.logger import _setup_logging, log
_setup_logging(config.log_level)

# ── Remaining imports ────────────────────────────────────────────────────────
from app.permissions import is_admin
from app.state import state
from core.events import bus
from ui.theme import apply_theme


def _set_dpi_awareness() -> None:
    """
    Must run before ANY Tk window is created — including the splash's.

    customtkinter sets Windows' per-process DPI awareness itself, but only
    as a side effect of constructing the first CTk() instance — which
    happens well after the splash's plain tk.Tk() has already been created
    and has already queried screen dimensions. That left the splash and
    the main window computing their centered position in two DIFFERENT
    coordinate spaces (virtualized vs physical pixels) whenever Windows
    display scaling isn't 100%, so the same "center on screen" math
    produced two different results.

    Setting it explicitly here, before the splash exists, makes both
    windows agree on what a pixel is. Windows only allows a process to set
    its DPI awareness ONCE per process lifetime (a second call raises) —
    and customtkinter's own internal call is NOT wrapped in a try/except,
    so simply calling SetProcessDpiAwareness ourselves first would make
    customtkinter's later call (inside ctk.CTk()) crash the app outright.
    customtkinter exposes deactivate_automatic_dpi_awareness() specifically
    for this — it tells customtkinter's ScalingTracker to skip its own
    awareness call entirely, so ours is the only one that ever runs.
    """
    if sys.platform != "win32":
        return
    try:
        import customtkinter as ctk
        ctk.deactivate_automatic_dpi_awareness()
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE — same value ctk itself uses
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()     # older Windows fallback
        except Exception:
            pass


def main() -> None:
    _set_dpi_awareness()
    log.info("DWUT starting up")

    # Admin check — offer to relaunch elevated if not already admin.
    # check_and_prompt() shows its own dialog (a temporary hidden root,
    # since no window exists yet this early) and, if accepted, hands off to
    # request_elevation() — which exits THIS process outright on success,
    # so the elevated relaunch starts main() completely fresh.
    from app.permissions import check_and_prompt
    check_and_prompt()
    state.is_admin = is_admin()
    if not state.is_admin:
        log.warning("Running without administrator privileges — some features will be unavailable")

    # Apply theme before building any CTk widgets (and before the splash,
    # so the splash matches whatever theme the user has set)
    apply_theme(config.theme)

    # Loading screen — fixed ~3s, blocks here (its own tiny mainloop) then
    # tears itself down before the real window is built. If the splash
    # fails for any reason (e.g. no display driver quirks) we skip it
    # rather than block startup on cosmetics.
    try:
        from ui.splash import show_splash
        show_splash()
    except Exception as exc:
        log.warning("Splash screen failed, skipping: %s", exc)

    # Late import of CTk window so theme is applied first
    try:
        from ui.window import MainWindow
        app = MainWindow()
        bus.set_root(app.root)
        log.info("Main window ready — entering Tk main loop")
        app.run()
    except ImportError as exc:
        # customtkinter not installed — show a plain tkinter fallback message
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            f"CustomTkinter is not installed.\n\n"
            f"Run: pip install customtkinter pillow psutil requests pymem pywin32\n\n"
            f"Error: {exc}",
        )
        sys.exit(1)
    except Exception as exc:
        log.critical("Fatal startup error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        from core.worker import worker
        state.shutting_down = True
        worker.shutdown(wait=False)
        log.info("DWUT shut down")


if __name__ == "__main__":
    main()
