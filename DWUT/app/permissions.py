"""
Admin / elevation handling.

is_admin()          — check current process elevation (ctypes, same as original)
request_elevation() — re-launch as admin via ShellExecuteW UAC prompt
"""

from __future__ import annotations

import ctypes
import sys


def is_admin() -> bool:
    """
    Return True if the current process has administrator privileges.

    Uses the same ctypes approach as the original codebase, which is the
    correct pattern for Windows UAC checks.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:
        # Not on Windows (dev machine / CI)
        return False


def request_elevation() -> None:
    """
    Re-launch the current script/EXE with UAC elevation (runas verb).

    This function does not return if elevation succeeds — the elevated process
    takes over.  If the user cancels the UAC prompt, this returns normally and
    the caller should handle the non-admin case.
    """
    try:
        import subprocess

        if getattr(sys, "frozen", False):
            # Running as PyInstaller EXE
            exe = sys.executable
            params = " ".join(sys.argv[1:])
        else:
            # Running from source
            exe = sys.executable
            params = " ".join([sys.argv[0]] + sys.argv[1:])

        ret = ctypes.windll.shell32.ShellExecuteW(
            None,       # hwnd
            "runas",    # verb — triggers UAC prompt
            exe,
            params,
            None,       # working directory (None = current)
            1,          # SW_NORMAL
        )

        # ShellExecuteW returns >32 on success
        if ret > 32:
            sys.exit(0)  # Old process exits, elevated one continues
        # If ret <= 32 user cancelled — fall through

    except Exception:
        pass


def check_and_prompt(root_window=None) -> bool:
    """
    Check for admin rights. If missing, show a dialog offering to
    re-launch with elevation.

    Returns True if already admin (or after successful elevation request —
    though in practice a successful elevation exits this process before
    returning at all, since request_elevation() calls sys.exit(0)).
    Returns False if the user declined, or elevation was cancelled/failed.

    `root_window` is optional — if given, used as the Tk parent for the
    dialog. If not given, a temporary hidden Tk root is created just for
    this dialog (there's no plain console fallback — this is a windowed
    app with no console attached in most launch configurations, so an
    input() prompt would never actually be visible to the user).
    """
    if is_admin():
        return True

    message = (
        "DWUT isn't running as Administrator.\n\n"
        "Most of its features — registry tweaks, service changes, the "
        "Startup Manager, repairs, and Windows features — need admin "
        "rights to work at all.\n\n"
        "Restart as Administrator now?"
    )

    owns_root = root_window is None
    if owns_root:
        import tkinter as tk
        root_window = tk.Tk()
        root_window.withdraw()

    try:
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Administrator Privileges Recommended", message, parent=root_window,
        )
    except Exception:
        answer = False
    finally:
        if owns_root:
            try:
                root_window.destroy()
            except Exception:
                pass

    if answer:
        request_elevation()   # exits this process outright on success

    return False
