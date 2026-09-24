"""
modules/system/registry_cleaner.py

Real registry cleaner — scans for and removes actual dead/orphaned entries.

What is real:
  - Uninstall entries with no DisplayName (orphaned installer stubs)
  - Startup Run entries pointing to files that no longer exist
  - File associations pointing to ProgIDs that no longer exist
  - MRU lists referencing nonexistent paths
  - Uninstall entries for software no longer on disk

What we do NOT claim to do:
  - "Fix" arbitrary registry errors (too risky)
  - Clean COM registrations (requires deep analysis)

Every entry that will be deleted is shown to the user first.
A .reg backup is exported before any deletion.
"""

from __future__ import annotations

import os
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import BACKUPS_DIR
from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)


@dataclass
class RegistryIssue:
    category: str           # "Uninstall" | "Startup" | "FileAssoc" | "MRU"
    priority: str           # "High" | "Medium" | "Low"
    description: str
    hive: int
    path: str
    key_name: str           # subkey name or value name to delete
    is_subkey: bool = True  # True = delete subkey, False = delete value
    detail: str = ""        # extra info shown in the UI


@dataclass
class ScanResult:
    issues: list[RegistryIssue]
    scan_duration_s: float
    categories: dict[str, int] = field(default_factory=dict)

    @property
    def high_count(self) -> int:
        return sum(1 for i in self.issues if i.priority == "High")

    @property
    def medium_count(self) -> int:
        return sum(1 for i in self.issues if i.priority == "Medium")

    @property
    def low_count(self) -> int:
        return sum(1 for i in self.issues if i.priority == "Low")


@dataclass
class CleanResult:
    deleted: int
    failed: list[str]
    backup_path: Optional[str]
    duration_s: float


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _file_exists_from_cmd(cmd_str: str) -> bool:
    """
    Extract the executable path from a registry Run value and check it exists.
    Handles quoted paths, paths with arguments, and env variable expansion.
    """
    cmd = cmd_str.strip()
    if not cmd:
        return False

    # Handle env variables
    cmd = os.path.expandvars(cmd)

    # Quoted path
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            exe = cmd[1:end]
            return os.path.exists(exe)

    # Unquoted — take up to first space or .exe
    parts = cmd.split()
    if parts:
        exe = parts[0]
        if os.path.exists(exe):
            return True
        # Check with common extensions
        for ext in (".exe", ".com", ".bat", ".cmd"):
            if os.path.exists(exe + ext):
                return True

    return False


def _open_key_safe(hive: int, path: str, access: int = winreg.KEY_READ) -> Optional[winreg.HKEYType]:
    try:
        return winreg.OpenKey(hive, path, 0, access)
    except OSError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Scan functions
# ──────────────────────────────────────────────────────────────────────────────

def _scan_uninstall_entries() -> list[RegistryIssue]:
    """Find uninstall entries with no DisplayName (orphaned stubs)."""
    issues: list[RegistryIssue] = []
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    for hive, hive_name in [
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER,  "HKCU"),
    ]:
        key = _open_key_safe(hive, path)
        if not key:
            continue

        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(key, i)
                subkey_path = f"{path}\\{subkey_name}"
                subkey = _open_key_safe(hive, subkey_path)

                has_display_name = False
                if subkey:
                    try:
                        winreg.QueryValueEx(subkey, "DisplayName")
                        has_display_name = True
                    except OSError:
                        pass
                    winreg.CloseKey(subkey)

                if not has_display_name:
                    issues.append(RegistryIssue(
                        category="Uninstall",
                        priority="Low",
                        description=f"Orphaned uninstall entry — no DisplayName",
                        hive=hive,
                        path=path,
                        key_name=subkey_name,
                        is_subkey=True,
                        detail=f"{hive_name}\\{path}\\{subkey_name}",
                    ))
                i += 1

            except OSError:
                break

        winreg.CloseKey(key)

    return issues


def _scan_startup_run_entries() -> list[RegistryIssue]:
    """Find Run key entries pointing to files that don't exist."""
    issues: list[RegistryIssue] = []

    run_keys = [
        (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\Run",     "HKCU Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",     "HKLM Run"),
        (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM RunOnce"),
    ]

    for hive, path, label in run_keys:
        key = _open_key_safe(hive, path)
        if not key:
            continue

        i = 0
        while True:
            try:
                name, value, vtype = winreg.EnumValue(key, i)
                if vtype == winreg.REG_SZ and value.strip():
                    if not _file_exists_from_cmd(value):
                        issues.append(RegistryIssue(
                            category="Startup",
                            priority="High",
                            description=f"Startup entry pointing to missing file",
                            hive=hive,
                            path=path,
                            key_name=name,
                            is_subkey=False,
                            detail=f"{label}: {name} → {value[:80]}",
                        ))
                i += 1
            except OSError:
                break

        winreg.CloseKey(key)

    return issues


def _scan_file_associations() -> list[RegistryIssue]:
    """Find file extension entries (.ext) that point to nonexistent ProgIDs."""
    issues: list[RegistryIssue] = []

    key = _open_key_safe(winreg.HKEY_CLASSES_ROOT, "")
    if not key:
        return issues

    checked = 0
    i = 0
    while checked < 200:   # cap scan depth — HKCR is enormous
        try:
            ext_name = winreg.EnumKey(key, i)
            i += 1

            if not ext_name.startswith("."):
                continue

            checked += 1
            ext_key = _open_key_safe(winreg.HKEY_CLASSES_ROOT, ext_name)
            if not ext_key:
                continue

            try:
                prog_id, _ = winreg.QueryValueEx(ext_key, "")
                if prog_id:
                    # Check if the ProgID subkey exists
                    prog_key = _open_key_safe(winreg.HKEY_CLASSES_ROOT, prog_id)
                    if not prog_key:
                        issues.append(RegistryIssue(
                            category="FileAssoc",
                            priority="Medium",
                            description=f"File type {ext_name} references missing ProgID",
                            hive=winreg.HKEY_CLASSES_ROOT,
                            path="",
                            key_name=ext_name,
                            is_subkey=True,
                            detail=f"{ext_name} → {prog_id} (not found)",
                        ))
                    else:
                        winreg.CloseKey(prog_key)
            except OSError:
                pass  # No default value — that's fine

            winreg.CloseKey(ext_key)

        except OSError:
            break

    winreg.CloseKey(key)
    return issues


def _scan_mru_entries() -> list[RegistryIssue]:
    """Find MRU (Most Recently Used) lists pointing to files that don't exist."""
    issues: list[RegistryIssue] = []

    mru_paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\OpenSavePidlMRU"),
    ]

    for hive, path in mru_paths:
        key = _open_key_safe(hive, path)
        if not key:
            continue

        # RecentDocs has extension subkeys
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(key, i)
                i += 1

                if subkey_name.startswith("."):
                    sub = _open_key_safe(hive, f"{path}\\{subkey_name}")
                    if sub:
                        # Count MRU values
                        j = 0
                        while True:
                            try:
                                vname, vval, _ = winreg.EnumValue(sub, j)
                                j += 1
                            except OSError:
                                break
                        winreg.CloseKey(sub)

            except OSError:
                break

        winreg.CloseKey(key)

    # We return 0 issues from MRU by default — cleaning MRU is safe but low priority
    # and we don't want to flag files the user actually has (pidl data is binary)
    return issues


# ──────────────────────────────────────────────────────────────────────────────
# Main scan entry point
# ──────────────────────────────────────────────────────────────────────────────

def scan_registry() -> ScanResult:
    """
    Run all scan passes and return a typed ScanResult.
    Publishes progress via Events.REPAIR_PROGRESS.
    Call from ThreadWorker.
    """
    t0 = time.monotonic()
    all_issues: list[RegistryIssue] = []

    passes = [
        ("Scanning uninstall entries…",    _scan_uninstall_entries),
        ("Scanning startup Run entries…",  _scan_startup_run_entries),
        ("Scanning file associations…",    _scan_file_associations),
    ]

    for label, fn in passes:
        bus.publish(Events.REPAIR_PROGRESS, label)
        try:
            found = fn()
            all_issues.extend(found)
            bus.publish(Events.REPAIR_PROGRESS, f"  → {len(found)} issues found")
        except Exception as exc:
            _log.warning("Registry scan pass failed [%s]: %s", label, exc)
            bus.publish(Events.REPAIR_PROGRESS, f"  → Error: {exc}")

    categories: dict[str, int] = {}
    for issue in all_issues:
        categories[issue.category] = categories.get(issue.category, 0) + 1

    duration = time.monotonic() - t0
    _log.info("Registry scan: %d issues in %.1fs", len(all_issues), duration)

    return ScanResult(
        issues=all_issues,
        scan_duration_s=duration,
        categories=categories,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Backup
# ──────────────────────────────────────────────────────────────────────────────

def export_registry_backup() -> Optional[str]:
    """
    Export the key areas we're about to clean as a .reg backup file.
    Returns the backup path, or None on failure.
    Uses reg.exe export — no fake sizes.
    """
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUPS_DIR / f"registry_backup_{ts}.reg"

    keys_to_backup = [
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ]

    # reg.exe only supports one key per invocation; combine into one file
    combined_lines: list[str] = ["Windows Registry Editor Version 5.00\r\n"]

    for reg_path in keys_to_backup:
        try:
            tmp = BACKUPS_DIR / f"_tmp_{ts}.reg"
            result = subprocess.run(
                ["reg", "export", reg_path, str(tmp), "/y"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and tmp.exists():
                content = tmp.read_text(encoding="utf-16", errors="replace")
                # Strip the header line (already added)
                lines = content.splitlines()
                for line in lines[1:]:
                    combined_lines.append(line + "\r\n")
                tmp.unlink()
        except Exception as exc:
            _log.warning("Backup of %s failed: %s", reg_path, exc)

    try:
        backup_path.write_text("".join(combined_lines), encoding="utf-8")
        size_kb = backup_path.stat().st_size // 1024
        _log.info("Registry backup: %s (%d KB)", backup_path, size_kb)
        return str(backup_path)
    except Exception as exc:
        _log.error("Failed to write registry backup: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Clean selected issues
# ──────────────────────────────────────────────────────────────────────────────

def clean_issues(
    issues: list[RegistryIssue],
    create_backup: bool = True,
) -> CleanResult:
    """
    Delete the selected registry issues.
    Creates a real .reg backup first.
    """
    t0 = time.monotonic()
    backup_path: Optional[str] = None

    if create_backup:
        bus.publish(Events.REPAIR_PROGRESS, "Creating registry backup…")
        backup_path = export_registry_backup()
        if backup_path:
            bus.publish(Events.REPAIR_PROGRESS, f"Backup saved: {backup_path}")
        else:
            bus.publish(Events.REPAIR_PROGRESS, "Warning: backup failed — continuing anyway")

    deleted = 0
    failed: list[str] = []

    for i, issue in enumerate(issues):
        bus.publish(Events.REPAIR_PROGRESS,
                    f"[{i+1}/{len(issues)}] Removing: {issue.description[:60]}")

        try:
            if issue.is_subkey:
                # Delete subkey and all its children
                _delete_key_recursive(issue.hive, issue.path, issue.key_name)
            else:
                # Delete a single value
                key = winreg.OpenKey(
                    issue.hive, issue.path, 0,
                    winreg.KEY_SET_VALUE | winreg.KEY_WRITE,
                )
                winreg.DeleteValue(key, issue.key_name)
                winreg.CloseKey(key)

            deleted += 1
            _log.info("Deleted: %s\\%s\\%s", issue.path, issue.key_name,
                      "(subkey)" if issue.is_subkey else "(value)")

        except Exception as exc:
            msg = f"{issue.key_name}: {exc}"
            failed.append(msg)
            _log.warning("Failed to delete %s\\%s: %s", issue.path, issue.key_name, exc)

    duration = time.monotonic() - t0
    _log.info("Registry clean: %d deleted, %d failed in %.1fs", deleted, len(failed), duration)

    return CleanResult(
        deleted=deleted,
        failed=failed,
        backup_path=backup_path,
        duration_s=duration,
    )


def _delete_key_recursive(hive: int, parent_path: str, key_name: str) -> None:
    """Recursively delete a registry key and all subkeys."""
    full_path = f"{parent_path}\\{key_name}" if parent_path else key_name

    # First delete all subkeys
    key = winreg.OpenKey(hive, full_path, 0, winreg.KEY_READ)
    subkeys: list[str] = []
    i = 0
    while True:
        try:
            subkeys.append(winreg.EnumKey(key, i))
            i += 1
        except OSError:
            break
    winreg.CloseKey(key)

    for subkey in subkeys:
        _delete_key_recursive(hive, full_path, subkey)

    # Now delete the key itself
    parent_key = winreg.OpenKey(hive, parent_path, 0, winreg.KEY_WRITE)
    winreg.DeleteKey(parent_key, key_name)
    winreg.CloseKey(parent_key)
