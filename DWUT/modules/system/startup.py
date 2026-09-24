"""
modules/system/startup.py

Real startup manager — reads HKCU/HKLM run keys and Task Scheduler entries.

Ported from StartupManagerTool (lines 16500–16856) and cleaned up.
No UI imports.  Returns typed dataclasses.  All changes logged.
"""

from __future__ import annotations

import os
import subprocess
import winreg
from dataclasses import dataclass
from typing import Optional

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)

LOC_USER_STARTUP  = "User Startup"
LOC_ALL_STARTUP   = "All-User Startup"


@dataclass
class StartupEntry:
    name: str
    command: str
    location: str          # e.g. "HKCU Run", "HKLM Run", "Task Scheduler", "User Startup"
    enabled: bool
    publisher: Optional[str] = None
    impact: str = "Unknown"   # "High" | "Medium" | "Low" | "Unknown"
    path: Optional[str] = None   # real filesystem path, for shell:startup entries only

    def impact_from_path(self) -> str:
        """Heuristic impact rating based on executable location."""
        cmd = self.command.lower()
        if any(x in cmd for x in ("update", "cloud", "sync", "onedrive", "dropbox")):
            return "Medium"
        if any(x in cmd for x in ("game", "steam", "discord", "spotify")):
            return "High"
        if any(x in cmd for x in ("helper", "agent", "tray")):
            return "Low"
        return "Unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Registry readers
# ──────────────────────────────────────────────────────────────────────────────

_RUN_KEYS = [
    (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\Run",         "HKCU Run"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run",         "HKLM Run"),
    (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\RunOnce",     "HKCU RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce",     "HKLM RunOnce"),
]

# Disabled entries are stored under a parallel key with "disabled" suffix
_DISABLED_PREFIX = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

# shell:startup folders — per-user and all-users
_STARTUP_FOLDERS: list[tuple[str, str]] = [
    (
        os.path.join(os.environ.get("APPDATA", ""),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"),
        LOC_USER_STARTUP,
    ),
    (
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"),
        LOC_ALL_STARTUP,
    ),
]

_DISABLED_SUFFIX = ".disabled"


def _read_startup_approved_state(hive: int, name: str) -> Optional[bool]:
    """
    Read the actual enabled/disabled marker for a Run-key value from
    StartupApproved\\Run — the SAME key Task Manager (and disable/enable
    below) write to. Returns None if Windows has never recorded an explicit
    state for this name (which means it's enabled — that's Windows' own
    default for an entry nobody has ever touched).

    The previous version never read this at all and just marked every
    registry-based entry "enabled" unconditionally, which is why a
    previously-disabled item still showed as Enabled here.
    """
    try:
        k = winreg.OpenKey(hive, _DISABLED_PREFIX, 0, winreg.KEY_READ)
        try:
            raw, _ = winreg.QueryValueEx(k, name)
            if isinstance(raw, (bytes, bytearray)) and len(raw) >= 1:
                return raw[0] != 0x03   # 0x03 = disabled; anything else (0x02 etc) = enabled
        finally:
            winreg.CloseKey(k)
    except Exception:
        pass
    return None


def _read_reg_run_key(hive: int, path: str, label: str) -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
                approved = _read_startup_approved_state(hive, name)
                entry = StartupEntry(
                    name=name,
                    command=value,
                    location=label,
                    enabled=approved if approved is not None else True,
                )
                entry.impact = entry.impact_from_path()
                entries.append(entry)
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass
    return entries


def _resolve_shortcut_target(lnk_path: str) -> Optional[str]:
    """Resolve a .lnk shortcut's target path + arguments via WScript.Shell (pywin32)."""
    try:
        import win32com.client  # pywin32 — optional dependency
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(lnk_path)
        target = (shortcut.Targetpath or "").strip()
        args = (shortcut.Arguments or "").strip()
        if not target:
            return None
        return f'"{target}" {args}'.strip() if args else target
    except Exception:
        return None


def _read_startup_folder(folder: str, label: str) -> list[StartupEntry]:
    """Read shortcuts/executables sitting directly in a shell:startup folder."""
    entries: list[StartupEntry] = []
    if not folder or not os.path.isdir(folder):
        return entries

    try:
        for fname in os.listdir(folder):
            if fname.lower() == "desktop.ini":
                continue
            full = os.path.join(folder, fname)
            if os.path.isdir(full):
                continue

            enabled = True
            display_name = fname
            if fname.lower().endswith(_DISABLED_SUFFIX):
                enabled = False
                display_name = fname[: -len(_DISABLED_SUFFIX)]

            base, ext = os.path.splitext(display_name)
            command = full
            if ext.lower() == ".lnk":
                resolved = _resolve_shortcut_target(full)
                if resolved:
                    command = resolved

            entry = StartupEntry(
                name=base,
                command=command,
                location=label,
                enabled=enabled,
                path=full,
            )
            entry.impact = entry.impact_from_path()
            entries.append(entry)
    except Exception as exc:
        _log.warning("Failed to read startup folder %s: %s", folder, exc)

    return entries


def _read_task_scheduler() -> list[StartupEntry]:
    """Read logon-triggered tasks from Task Scheduler via schtasks CLI."""
    entries: list[StartupEntry] = []
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "CSV", "/v"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        lines = result.stdout.splitlines()
        if not lines:
            return entries

        headers = [h.strip('"') for h in lines[0].split(",")]
        try:
            idx_name   = headers.index("TaskName")
            idx_status = headers.index("Status")
            idx_trigger= headers.index("Scheduled Task State")
            idx_action = headers.index("Task To Run")
        except ValueError:
            return entries

        for line in lines[1:]:
            parts = [p.strip('"') for p in line.split(",")]
            if len(parts) <= max(idx_name, idx_status, idx_trigger, idx_action):
                continue
            trigger = parts[idx_trigger] if idx_trigger < len(parts) else ""
            if "At log on" not in trigger and "Logon" not in trigger:
                continue
            name   = parts[idx_name]
            status = parts[idx_status]
            action = parts[idx_action] if idx_action < len(parts) else ""

            entry = StartupEntry(
                name=name.split("\\")[-1],
                command=action,
                location="Task Scheduler",
                enabled=(status.lower() == "ready"),
            )
            entry.impact = entry.impact_from_path()
            entries.append(entry)

    except Exception as exc:
        _log.warning("Task Scheduler read failed: %s", exc)

    return entries


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_startup_entries() -> list[StartupEntry]:
    """Return all startup entries from registry and Task Scheduler."""
    entries: list[StartupEntry] = []

    for hive, path, label in _RUN_KEYS:
        entries.extend(_read_reg_run_key(hive, path, label))

    for folder, label in _STARTUP_FOLDERS:
        entries.extend(_read_startup_folder(folder, label))

    entries.extend(_read_task_scheduler())

    _log.info("Loaded %d startup entries", len(entries))
    bus.publish(Events.STARTUP_LOADED, entries)
    return entries


def disable_startup_entry(entry: StartupEntry) -> bool:
    """
    Disable a startup entry.

    For registry run keys: moves the value under the StartupApproved key
    (same as Task Manager — doesn't delete, easy to re-enable).
    For Task Scheduler: uses schtasks /change /disable.
    """
    if entry.location in (LOC_USER_STARTUP, LOC_ALL_STARTUP):
        return _toggle_startup_folder_entry(entry, disable=True)

    if entry.location == "Task Scheduler":
        try:
            result = subprocess.run(
                ["schtasks", "/change", "/tn", entry.name, "/disable"],
                capture_output=True, timeout=10,
            )
            ok = result.returncode == 0
            if ok:
                _log.info("Disabled task: %s", entry.name)
            return ok
        except Exception as exc:
            _log.error("Failed to disable task %s: %s", entry.name, exc)
            return False

    # Registry entry — use StartupApproved key (what Task Manager uses).
    # CreateKeyEx (not OpenKey) because this subkey doesn't always exist
    # yet — HKLM's StartupApproved\Run in particular is only created
    # lazily by Explorer/Task Manager the first time something toggles an
    # entry there. OpenKey raises FileNotFoundError in that case, which
    # was silently swallowed below and made Disable a no-op whenever that
    # key hadn't been created yet.
    hive = winreg.HKEY_CURRENT_USER if "HKCU" in entry.location else winreg.HKEY_LOCAL_MACHINE
    try:
        k = winreg.CreateKeyEx(hive, _DISABLED_PREFIX, 0, winreg.KEY_SET_VALUE)
        # Value is 12 bytes, first byte 03 = disabled (rest is a Windows FILETIME
        # timestamp Task Manager writes; zeros are accepted fine)
        disabled_marker = bytes([0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        winreg.SetValueEx(k, entry.name, 0, winreg.REG_BINARY, disabled_marker)
        winreg.CloseKey(k)
        entry.enabled = False
        _log.info("Disabled startup entry: %s", entry.name)
        bus.publish(Events.STARTUP_CHANGED, entry.name)
        return True
    except Exception as exc:
        _log.error("Failed to disable %s: %s", entry.name, exc)
        return False


def _toggle_startup_folder_entry(entry: StartupEntry, disable: bool) -> bool:
    """
    Disable/enable a shell:startup entry by renaming the file with/without a
    '.disabled' suffix — same non-destructive idea as the registry path.
    """
    if not entry.path or not os.path.exists(entry.path):
        _log.error("Startup folder entry missing on disk: %s", entry.name)
        return False

    is_disabled = entry.path.lower().endswith(_DISABLED_SUFFIX)
    if disable == is_disabled:
        return True  # already in the desired state

    new_path = (
        entry.path + _DISABLED_SUFFIX if disable
        else entry.path[: -len(_DISABLED_SUFFIX)]
    )

    try:
        os.rename(entry.path, new_path)
        entry.path = new_path
        entry.enabled = not disable
        _log.info("%s startup folder entry: %s", "Disabled" if disable else "Enabled", entry.name)
        bus.publish(Events.STARTUP_CHANGED, entry.name)
        return True
    except Exception as exc:
        _log.error("Failed to %s startup folder entry %s: %s",
                    "disable" if disable else "enable", entry.name, exc)
        return False


def enable_startup_entry(entry: StartupEntry) -> bool:
    """Re-enable a previously disabled startup entry."""
    if entry.location in (LOC_USER_STARTUP, LOC_ALL_STARTUP):
        return _toggle_startup_folder_entry(entry, disable=False)

    if entry.location == "Task Scheduler":
        try:
            result = subprocess.run(
                ["schtasks", "/change", "/tn", entry.name, "/enable"],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    hive = winreg.HKEY_CURRENT_USER if "HKCU" in entry.location else winreg.HKEY_LOCAL_MACHINE
    try:
        k = winreg.CreateKeyEx(hive, _DISABLED_PREFIX, 0, winreg.KEY_SET_VALUE)
        # Value: 02 00 00 00 00 00 00 00 00 00 00 00 = enabled
        enabled_marker = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        winreg.SetValueEx(k, entry.name, 0, winreg.REG_BINARY, enabled_marker)
        winreg.CloseKey(k)
        entry.enabled = True
        _log.info("Enabled startup entry: %s", entry.name)
        bus.publish(Events.STARTUP_CHANGED, entry.name)
        return True
    except Exception as exc:
        _log.error("Failed to enable %s: %s", entry.name, exc)
        return False


def open_file_location(entry: StartupEntry) -> None:
    """Open the folder containing the startup item in Explorer."""
    try:
        if entry.path:
            # shell:startup entries — open the folder the .lnk/.exe actually lives in
            folder = os.path.dirname(entry.path)
        else:
            cmd = entry.command.strip('"').split('"')[0]
            folder = os.path.dirname(cmd)
        if os.path.isdir(folder):
            subprocess.Popen(["explorer", folder])
    except Exception as exc:
        _log.warning("Could not open file location for %s: %s", entry.name, exc)
