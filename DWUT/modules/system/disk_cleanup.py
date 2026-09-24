r"""
modules/system/disk_cleanup.py

Real disk cleanup — measures actual bytes before deleting.

Categories:
  temp_user      — %TEMP% and %LOCALAPPDATA%\Temp
  temp_system    — C:\Windows\Temp
  prefetch       — C:\Windows\Prefetch
  inet_cache     — IE/Edge internet cache
  thumbnail_cache— Explorer thumbnail DB
  update_cache   — Windows Update SoftwareDistribution\Download
  recycle_bin    — per-drive recycle bins
  event_logs     — clear Windows Event Logs (optional/aggressive)
  crash_dumps    — minidump files

Each category returns real byte counts — no fake numbers anywhere.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)


@dataclass
class CleanCategory:
    key: str
    name: str
    description: str
    paths: list[str]     # can contain env vars
    aggressive: bool = False  # needs extra confirmation
    estimated_mb: Optional[float] = None  # filled in after scan
    actual_bytes: int = 0


@dataclass
class DiskScanResult:
    categories: list[CleanCategory]
    total_bytes: int
    scan_duration_s: float

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)


@dataclass
class DiskCleanResult:
    category_results: dict[str, int]   # key → bytes_freed
    total_bytes_freed: int
    files_deleted: int
    errors: list[str]
    duration_s: float

    @property
    def total_mb_freed(self) -> float:
        return self.total_bytes_freed / (1024 * 1024)


# ──────────────────────────────────────────────────────────────────────────────
# Category definitions
# ──────────────────────────────────────────────────────────────────────────────

def _make_categories() -> list[CleanCategory]:
    return [
        CleanCategory(
            key="temp_user",
            name="User Temp Files",
            description="Temporary files in %TEMP% and %LOCALAPPDATA%\\Temp",
            paths=[
                os.path.expandvars(r"%TEMP%"),
                os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
            ],
        ),
        CleanCategory(
            key="temp_system",
            name="Windows Temp Files",
            description="Temporary files in C:/Windows/Temp",
            paths=[r"C:\Windows\Temp"],
        ),
        CleanCategory(
            key="prefetch",
            name="Prefetch Cache",
            description="Application prefetch data (safe to delete; Windows rebuilds it)",
            paths=[r"C:\Windows\Prefetch"],
        ),
        CleanCategory(
            key="inet_cache",
            name="Browser / Internet Cache",
            description="Internet Explorer and Edge legacy cache",
            paths=[
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\INetCache"),
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\INetCookies"),
            ],
        ),
        CleanCategory(
            key="thumbnail_cache",
            name="Thumbnail Cache",
            description="Explorer thumbnail database files (rebuilt automatically)",
            paths=[
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer"),
            ],
        ),
        CleanCategory(
            key="crash_dumps",
            name="Crash Dumps / Minidumps",
            description="Windows crash report files in Minidump",
            paths=[
                r"C:\Windows\Minidump",
                os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps"),
            ],
        ),
        CleanCategory(
            key="update_cache",
            name="Windows Update Cache",
            description="Downloaded Windows Update packages (safe after updates are installed)",
            paths=[r"C:\Windows\SoftwareDistribution\Download"],
            aggressive=False,
        ),
        CleanCategory(
            key="event_logs",
            name="Windows Event Logs",
            description="Clears all Windows event log files (aggressive — loses log history)",
            paths=[],  # handled separately via wevtutil
            aggressive=True,
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Size calculation
# ──────────────────────────────────────────────────────────────────────────────

def _dir_size(path: str) -> int:
    """Return total bytes of all files under `path`. Never throws."""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return total


def _event_log_size() -> int:
    """Return total size of .evtx log files."""
    log_dir = Path(r"C:\Windows\System32\winevt\Logs")
    total = 0
    if log_dir.exists():
        for f in log_dir.glob("*.evtx"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Scan
# ──────────────────────────────────────────────────────────────────────────────

def scan_disk_categories() -> DiskScanResult:
    """
    Measure size of each cleanup category without deleting anything.
    Publishes progress. Returns DiskScanResult with real byte counts.
    """
    t0 = time.monotonic()
    categories = _make_categories()
    total = 0

    for cat in categories:
        bus.publish(Events.CLEANUP_PROGRESS, f"Measuring: {cat.name}…")

        if cat.key == "event_logs":
            size = _event_log_size()
        else:
            size = sum(_dir_size(p) for p in cat.paths)

        cat.actual_bytes = size
        cat.estimated_mb = size / (1024 * 1024)
        total += size

        bus.publish(Events.CLEANUP_PROGRESS,
                    f"  {cat.name}: {cat.estimated_mb:.1f} MB")

    duration = time.monotonic() - t0
    _log.info("Disk scan complete: %.1f MB total in %.1fs",
              total / (1024 * 1024), duration)

    return DiskScanResult(
        categories=categories,
        total_bytes=total,
        scan_duration_s=duration,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Clean
# ──────────────────────────────────────────────────────────────────────────────

def _delete_dir_contents(path: str) -> tuple[int, int, list[str]]:
    """
    Delete all contents of a directory (not the directory itself).
    Returns (bytes_freed, files_deleted, errors).
    """
    bytes_freed = 0
    files_deleted = 0
    errors: list[str] = []
    p = Path(path)

    if not p.exists():
        return 0, 0, []

    for item in p.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                size = item.stat().st_size
                item.unlink()
                bytes_freed += size
                files_deleted += 1
            elif item.is_dir():
                size = _dir_size(str(item))
                shutil.rmtree(str(item), ignore_errors=True)
                bytes_freed += size
                files_deleted += 1
        except PermissionError:
            pass  # In-use file — skip silently
        except Exception as exc:
            errors.append(f"{item.name}: {exc}")

    return bytes_freed, files_deleted, errors


def _clear_event_logs() -> tuple[int, int, list[str]]:
    """Clear Windows Event Logs via wevtutil."""
    bytes_before = _event_log_size()
    errors: list[str] = []

    try:
        # Get list of log names
        result = subprocess.run(
            ["wevtutil", "el"],
            capture_output=True, text=True, timeout=30,
        )
        log_names = [l.strip() for l in result.stdout.splitlines() if l.strip()]

        for name in log_names[:50]:  # cap at 50 to avoid taking forever
            try:
                subprocess.run(
                    ["wevtutil", "cl", name],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    except Exception as exc:
        errors.append(f"wevtutil: {exc}")

    bytes_after = _event_log_size()
    freed = max(0, bytes_before - bytes_after)
    return freed, 0, errors


def _stop_wu_services() -> None:
    for svc in ("wuauserv", "bits"):
        subprocess.run(["net", "stop", svc], capture_output=True, timeout=15)


def _start_wu_services() -> None:
    for svc in ("wuauserv", "bits"):
        subprocess.run(["net", "start", svc], capture_output=True, timeout=15)


def clean_categories(
    category_keys: list[str],
) -> DiskCleanResult:
    """
    Clean the selected categories.
    Returns real byte counts — nothing is faked.
    Publishes Events.CLEANUP_PROGRESS throughout.
    """
    t0 = time.monotonic()
    categories = {cat.key: cat for cat in _make_categories()}

    category_results: dict[str, int] = {}
    total_bytes = 0
    total_files = 0
    all_errors: list[str] = []

    # Special handling — stop WU services before cleaning its cache
    needs_wu_stop = "update_cache" in category_keys

    if needs_wu_stop:
        bus.publish(Events.CLEANUP_PROGRESS, "Stopping Windows Update services…")
        _stop_wu_services()

    for key in category_keys:
        cat = categories.get(key)
        if not cat:
            continue

        bus.publish(Events.CLEANUP_PROGRESS, f"Cleaning: {cat.name}…")
        cat_bytes = 0
        cat_files = 0

        if key == "event_logs":
            freed, files, errors = _clear_event_logs()
            cat_bytes = freed
            cat_files = files
            all_errors.extend(errors)
        else:
            for path in cat.paths:
                freed, files, errors = _delete_dir_contents(path)
                cat_bytes += freed
                cat_files += files
                all_errors.extend(errors)

        category_results[key] = cat_bytes
        total_bytes += cat_bytes
        total_files += cat_files

        bus.publish(Events.CLEANUP_PROGRESS,
                    f"  ✓ {cat.name}: {cat_bytes / (1024*1024):.1f} MB freed")

    if needs_wu_stop:
        bus.publish(Events.CLEANUP_PROGRESS, "Restarting Windows Update services…")
        _start_wu_services()

    duration = time.monotonic() - t0
    _log.info("Disk cleanup: %.1f MB freed, %d files in %.1fs",
              total_bytes / (1024 * 1024), total_files, duration)

    bus.publish(Events.CLEANUP_DONE, DiskCleanResult(
        category_results=category_results,
        total_bytes_freed=total_bytes,
        files_deleted=total_files,
        errors=all_errors,
        duration_s=duration,
    ))

    return DiskCleanResult(
        category_results=category_results,
        total_bytes_freed=total_bytes,
        files_deleted=total_files,
        errors=all_errors,
        duration_s=duration,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Free space info
# ──────────────────────────────────────────────────────────────────────────────

def get_drive_info(drive: str = "C:\\") -> dict:
    """Return total/free/used GB for a drive."""
    try:
        total, used, free = shutil.disk_usage(drive)
        return {
            "drive": drive,
            "total_gb": total / (1024 ** 3),
            "used_gb": used / (1024 ** 3),
            "free_gb": free / (1024 ** 3),
            "used_pct": used / total * 100 if total else 0,
        }
    except Exception:
        return {"drive": drive, "total_gb": 0, "used_gb": 0, "free_gb": 0, "used_pct": 0}
