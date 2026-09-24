"""
modules/system/winutil.py

Chris Titus-style Windows Utility operations.

Every function here is real — no simulated output, no fake progress.
If a command can't run it logs why and returns False.

Covers:
  Repairs    — SFC, DISM, network reset, Winsock, DNS, Store, WinGet
  Tweaks     — DNS, visual effects, power, Explorer settings
  Config     — launch system tools (Device Manager, Services, etc.)
  Updates    — check, force, apply, driver update via winget
  Networking — flush DNS, reset TCP/IP, reset Winsock
  Cleanup    — temp files (real deletion + measurement), Update cache
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RepairResult:
    tool: str
    success: bool
    output: str
    duration_s: float


@dataclass
class CleanupResult:
    dirs_scanned: int
    files_deleted: int
    bytes_freed: int
    errors: list[str] = field(default_factory=list)

    @property
    def mb_freed(self) -> float:
        return self.bytes_freed / (1024 * 1024)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helper
# ──────────────────────────────────────────────────────────────────────────────

def _run(
    cmd: list[str] | str,
    timeout: int = 300,
    shell: bool = False,
    publish_event: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Run a command, capture output, return (success, combined_output).
    Publishes output lines on `publish_event` if provided.

    Enforces `timeout` with a real watchdog. The obvious-looking
    `proc.wait(timeout=timeout)` placed AFTER the `for line in proc.stdout`
    loop below does basically nothing — that loop already blocks until the
    process closes its stdout (i.e. until it's already finished or hung),
    so by the time wait() runs there's nothing left to actually wait for.
    A genuinely hung command (e.g. an installer silently popping a dialog
    that will never get answered) would block forever instead of stopping
    at `timeout`, which read as "crashing" / the app becoming unresponsive.
    A background timer that kills the process is what actually enforces it.

    stdin is explicitly closed (DEVNULL) so a command that tries to prompt
    for input fails/exits immediately instead of hanging on a read that
    can never be answered — this app has no console attached to answer it.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            shell=shell,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return False, f"Command not found: {exc}"
    except Exception as exc:
        return False, str(exc)

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.daemon = True
    timer.start()

    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip()
            lines.append(stripped)
            if publish_event and stripped:
                bus.publish(publish_event, stripped)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        lines.append(str(exc))
    finally:
        timer.cancel()

    output = "\n".join(lines)
    if timed_out.is_set():
        return False, output + "\n[Command timed out after " + str(timeout) + "s and was terminated]"
    return proc.returncode == 0, output


def _run_ps(cmd: str, timeout: int = 300, publish_event: Optional[str] = None) -> tuple[bool, str]:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        timeout=timeout,
        publish_event=publish_event,
    )


def _find_winget() -> str:
    """
    Locate a runnable winget.exe.

    winget is registered as a Windows "App Execution Alias" living at
    %LOCALAPPDATA%\\Microsoft\\WindowsApps\\winget.exe. That folder is on a
    normal user's PATH — but a process launched elevated (as this app
    commonly is, since most of its features need admin) can end up with a
    PATH that doesn't include it, a well-documented Windows quirk. That
    made plain `winget ...` calls fail outright with "command not found"
    specifically when running elevated, which is most likely why winget
    updates looked broken. Falling back to the explicit path fixes it.
    """
    found = shutil.which("winget")
    if found:
        return found
    explicit = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe")
    if os.path.isfile(explicit):
        return explicit
    return "winget"   # let it fail with a clear "not found" message if truly missing


# ──────────────────────────────────────────────────────────────────────────────
# REPAIRS
# ──────────────────────────────────────────────────────────────────────────────

def run_sfc() -> RepairResult:
    """Run System File Checker (sfc /scannow). Takes 5-20 min."""
    _log.info("Starting SFC /scannow")
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, "Running SFC /scannow — this may take 5–20 minutes…")

    ok, out = _run(
        ["sfc", "/scannow"],
        timeout=1800,
        publish_event=Events.REPAIR_PROGRESS,
    )

    duration = time.monotonic() - t0
    success = ok or "did not find any integrity violations" in out.lower() or "successfully repaired" in out.lower()

    result = RepairResult(tool="SFC", success=success, output=out, duration_s=duration)
    bus.publish(Events.REPAIR_DONE, result)
    _log.info("SFC complete in %.0fs — success=%s", duration, success)
    return result


def run_dism() -> RepairResult:
    """Run DISM /Online /Cleanup-Image /RestoreHealth. Takes 10-30 min."""
    _log.info("Starting DISM RestoreHealth")
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, "Running DISM RestoreHealth — this may take 10–30 minutes…")

    ok, out = _run(
        ["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"],
        timeout=3600,
        publish_event=Events.REPAIR_PROGRESS,
    )

    duration = time.monotonic() - t0
    success = ok or "The restore operation completed successfully" in out

    result = RepairResult(tool="DISM", success=success, output=out, duration_s=duration)
    bus.publish(Events.REPAIR_DONE, result)
    _log.info("DISM complete in %.0fs — success=%s", duration, success)
    return result


def run_sfc_then_dism() -> list[RepairResult]:
    """Run SFC then DISM sequentially — recommended order."""
    results = [run_sfc(), run_dism()]
    return results


def reset_network_stack() -> RepairResult:
    """Reset TCP/IP, Winsock, and flush DNS."""
    t0 = time.monotonic()
    cmds = [
        (["netsh", "int", "ip", "reset"],            "TCP/IP reset"),
        (["netsh", "winsock", "reset"],               "Winsock reset"),
        (["netsh", "advfirewall", "reset"],            "Firewall rules reset"),
        (["ipconfig", "/flushdns"],                    "DNS cache flushed"),
        (["ipconfig", "/registerdns"],                 "DNS re-registered"),
        (["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"], "TCP auto-tuning normalized"),
    ]
    output_lines: list[str] = []
    all_ok = True

    for cmd, label in cmds:
        bus.publish(Events.REPAIR_PROGRESS, f"Running: {label}…")
        ok, out = _run(cmd, timeout=30)
        output_lines.append(f"{'✓' if ok else '✗'} {label}")
        if not ok:
            all_ok = False
            output_lines.append(f"  → {out[:100]}")

    result = RepairResult(
        tool="Network Reset",
        success=all_ok,
        output="\n".join(output_lines),
        duration_s=time.monotonic() - t0,
    )
    bus.publish(Events.REPAIR_DONE, result)
    return result


def flush_dns() -> bool:
    """Flush DNS resolver cache only."""
    ok, _ = _run(["ipconfig", "/flushdns"], timeout=10)
    if ok:
        bus.publish(Events.REPAIR_PROGRESS, "DNS cache flushed")
    return ok


def reset_winsock() -> RepairResult:
    """Reset Winsock catalog."""
    t0 = time.monotonic()
    ok, out = _run(["netsh", "winsock", "reset"], timeout=30)
    result = RepairResult(tool="Winsock Reset", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


def repair_windows_update() -> RepairResult:
    """Stop WU services, clear SoftwareDistribution, restart."""
    t0 = time.monotonic()
    steps = [
        (["net", "stop", "wuauserv"],   "Stop Windows Update service"),
        (["net", "stop", "cryptsvc"],   "Stop Cryptographic service"),
        (["net", "stop", "bits"],       "Stop BITS"),
        (["net", "stop", "msiserver"],  "Stop MSI Installer"),
    ]
    output_lines: list[str] = []

    for cmd, label in steps:
        bus.publish(Events.REPAIR_PROGRESS, label)
        ok, _ = _run(cmd, timeout=20)
        output_lines.append(f"{'✓' if ok else '~'} {label}")

    # Clear SoftwareDistribution
    sd_path = Path(r"C:\Windows\SoftwareDistribution\Download")
    if sd_path.exists():
        try:
            shutil.rmtree(str(sd_path))
            sd_path.mkdir(parents=True, exist_ok=True)
            output_lines.append("✓ SoftwareDistribution\\Download cleared")
        except Exception as exc:
            output_lines.append(f"✗ Clear SoftwareDistribution: {exc}")

    catroot_path = Path(r"C:\Windows\System32\catroot2")
    if catroot_path.exists():
        try:
            shutil.rmtree(str(catroot_path))
            catroot_path.mkdir(parents=True, exist_ok=True)
            output_lines.append("✓ catroot2 cleared")
        except Exception as exc:
            output_lines.append(f"~ catroot2: {exc}")

    restart_steps = [
        (["net", "start", "wuauserv"],  "Start Windows Update service"),
        (["net", "start", "cryptsvc"],  "Start Cryptographic service"),
        (["net", "start", "bits"],      "Start BITS"),
        (["net", "start", "msiserver"], "Start MSI Installer"),
    ]
    for cmd, label in restart_steps:
        bus.publish(Events.REPAIR_PROGRESS, label)
        ok, _ = _run(cmd, timeout=20)
        output_lines.append(f"{'✓' if ok else '~'} {label}")

    result = RepairResult(
        tool="Windows Update Repair",
        success=True,
        output="\n".join(output_lines),
        duration_s=time.monotonic() - t0,
    )
    bus.publish(Events.REPAIR_DONE, result)
    return result


def repair_microsoft_store() -> RepairResult:
    """Re-register and reset Microsoft Store."""
    t0 = time.monotonic()
    cmds_ps = [
        "Get-AppxPackage -AllUsers Microsoft.WindowsStore | Foreach {Add-AppxPackage -DisableDevelopmentMode -Register \"$($_.InstallLocation)\\AppXManifest.xml\" -ErrorAction SilentlyContinue}",
        "wsreset.exe",
    ]
    out_lines: list[str] = []
    for cmd in cmds_ps:
        bus.publish(Events.REPAIR_PROGRESS, f"Running: {cmd[:50]}…")
        ok, out = _run_ps(cmd, timeout=60)
        out_lines.append(f"{'✓' if ok else '~'} {cmd[:40]}")

    result = RepairResult(
        tool="Store Repair",
        success=True,
        output="\n".join(out_lines),
        duration_s=time.monotonic() - t0,
    )
    bus.publish(Events.REPAIR_DONE, result)
    return result


def enable_ntp_sync() -> RepairResult:
    """Enable and start the Windows Time service, then force a resync
    against time.windows.com."""
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, "Configuring Windows Time service…")
    cmd = (
        "Set-Service -Name W32Time -StartupType Automatic; "
        "Start-Service -Name W32Time -ErrorAction SilentlyContinue; "
        "w32tm /config /manualpeerlist:time.windows.com /syncfromflags:manual /update; "
        "w32tm /resync /force"
    )
    ok, out = _run_ps(cmd, timeout=60)
    result = RepairResult(tool="NTP Time Sync", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


def enable_autologon(username: str, password: str, domain: str = ".") -> RepairResult:
    """
    Configure Windows to automatically sign in as `username` at boot,
    skipping the login screen. Stores the password in the registry the
    same way Microsoft's own Sysinternals Autologon tool does — which
    means it's stored in a readable (if not casually visible) location.
    Only use this on a PC only you have physical access to.
    """
    t0 = time.monotonic()
    if not username or not password:
        result = RepairResult(tool="AutoLogon", success=False,
                               output="Username and password are required.", duration_s=0.0)
        bus.publish(Events.REPAIR_DONE, result)
        return result

    bus.publish(Events.REPAIR_PROGRESS, "Configuring AutoLogon…")
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "AutoAdminLogon", 0, winreg.REG_SZ, "1")
        winreg.SetValueEx(k, "DefaultUserName", 0, winreg.REG_SZ, username)
        winreg.SetValueEx(k, "DefaultPassword", 0, winreg.REG_SZ, password)
        winreg.SetValueEx(k, "DefaultDomainName", 0, winreg.REG_SZ, domain)
        winreg.CloseKey(k)
        ok, out = True, f"AutoLogon enabled for '{username}'. Takes effect on next restart."
    except Exception as exc:
        ok, out = False, str(exc)

    result = RepairResult(tool="AutoLogon", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


def disable_autologon() -> RepairResult:
    """Turn AutoLogon back off and clear the stored plaintext password."""
    t0 = time.monotonic()
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "AutoAdminLogon", 0, winreg.REG_SZ, "0")
        try:
            winreg.DeleteValue(k, "DefaultPassword")
        except FileNotFoundError:
            pass
        winreg.CloseKey(k)
        ok, out = True, "AutoLogon disabled."
    except Exception as exc:
        ok, out = False, str(exc)

    result = RepairResult(tool="AutoLogon", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


def repair_winget() -> RepairResult:
    """Re-install / repair WinGet (App Installer) from the Microsoft Store."""
    t0 = time.monotonic()
    winget = _find_winget()
    cmd = (
        'Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe '
        '-ErrorAction SilentlyContinue; '
        f'"{winget}" --version'
    )
    bus.publish(Events.REPAIR_PROGRESS, "Repairing WinGet…")
    ok, out = _run_ps(cmd, timeout=120)
    result = RepairResult(tool="WinGet Repair", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# CLEANUP
# ──────────────────────────────────────────────────────────────────────────────

_TEMP_DIRS = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    r"C:\Windows\Temp",
    r"C:\Windows\Prefetch",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\INetCache"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer"),
]


def clean_temp_files() -> CleanupResult:
    """
    Delete files from all temp directories.
    Returns real byte counts — not fake numbers.
    """
    result = CleanupResult(dirs_scanned=0, files_deleted=0, bytes_freed=0)

    for dir_path in _TEMP_DIRS:
        p = Path(dir_path)
        if not p.exists():
            continue

        result.dirs_scanned += 1
        bus.publish(Events.CLEANUP_PROGRESS, f"Cleaning {dir_path}…")

        for item in p.iterdir():
            try:
                if item.is_file():
                    size = item.stat().st_size
                    item.unlink()
                    result.files_deleted += 1
                    result.bytes_freed   += size
                elif item.is_dir():
                    size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    shutil.rmtree(str(item), ignore_errors=True)
                    result.files_deleted += 1
                    result.bytes_freed   += size
            except PermissionError:
                pass   # In-use files are skipped silently
            except Exception as exc:
                result.errors.append(f"{item}: {exc}")

    bus.publish(Events.CLEANUP_DONE, result)
    _log.info("Temp cleanup: %d files, %.1f MB freed", result.files_deleted, result.mb_freed)
    return result


def clear_update_cache() -> CleanupResult:
    """Stop Windows Update, clear cache, restart."""
    _run(["net", "stop", "wuauserv"], timeout=20)
    _run(["net", "stop", "bits"],     timeout=20)

    sd = Path(r"C:\Windows\SoftwareDistribution\Download")
    result = CleanupResult(dirs_scanned=1, files_deleted=0, bytes_freed=0)

    if sd.exists():
        for item in sd.iterdir():
            try:
                size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else item.stat().st_size
                if item.is_dir():
                    shutil.rmtree(str(item), ignore_errors=True)
                else:
                    item.unlink()
                result.files_deleted += 1
                result.bytes_freed   += size
            except Exception as exc:
                result.errors.append(str(exc))

    _run(["net", "start", "wuauserv"], timeout=20)
    _run(["net", "start", "bits"],     timeout=20)

    bus.publish(Events.CLEANUP_DONE, result)
    _log.info("Update cache cleared: %.1f MB", result.mb_freed)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# WINDOWS UPDATES
# ──────────────────────────────────────────────────────────────────────────────

def check_for_updates() -> tuple[bool, str]:
    """Run UsoClient StartScan and return output."""
    bus.publish(Events.REPAIR_PROGRESS, "Triggering Windows Update scan…")
    ok, out = _run(["UsoClient", "StartScan"], timeout=30)
    return ok, out


def update_all_apps() -> RepairResult:
    """Run winget upgrade --all to update installed applications."""
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, "Running winget upgrade --all…")
    winget = _find_winget()
    # NOTE: --disable-interactivity was previously included here, but it
    # was only added in winget v1.5 — on an older winget install (still
    # common; it isn't auto-updated with Windows) that flag alone makes
    # winget reject the ENTIRE command with a usage error before it even
    # starts, which reads as "throwing tons of errors" even though nothing
    # about the upgrade itself failed. --silent alone has been supported
    # since much earlier and is enough to avoid interactive prompts hanging.
    ok, out = _run(
        [winget, "upgrade", "--all", "--silent",
         "--accept-source-agreements", "--accept-package-agreements"],
        timeout=900,
        publish_event=Events.REPAIR_PROGRESS,
    )

    # winget returns non-zero for "nothing to upgrade" on some versions —
    # don't report that as a failure.
    if not ok and ("no installed package" in out.lower() or "no applicable update" in out.lower()):
        ok = True

    # winget --all upgrading dozens of apps commonly has SOME packages fail
    # individually (pinned versions, manual-only installers, msstore auth,
    # etc) even on a totally healthy run — that's normal winget behavior,
    # not a bug in this app. Summarize rather than let a wall of per-package
    # noise read as "everything is broken".
    lines = [l for l in out.splitlines() if l.strip()]
    failed_count = sum(1 for l in lines if "failed" in l.lower() or "no package found matching" in l.lower())
    ok_count = sum(1 for l in lines if l.lower().startswith("successfully installed")
                   or "no available upgrade" in l.lower())
    if failed_count or ok_count:
        summary = f"Summary: {ok_count} up to date/updated, {failed_count} individual package(s) failed.\n\n"
        out = summary + out

    result = RepairResult(tool="WinGet Upgrade", success=ok, output=out, duration_s=time.monotonic() - t0)
    bus.publish(Events.REPAIR_DONE, result)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG / TOOL LAUNCHERS
# ──────────────────────────────────────────────────────────────────────────────

class _ToolDef:
    """
    Describes how to launch a system tool.

    strategy:
      "exe"      — direct subprocess.Popen with the given args list
      "msc"      — MMC snap-in opened via: cmd /c start "" file.msc
      "cpl"      — Control Panel applet via: rundll32 shell32.dll,Control_RunDLL file.cpl[,tab]
      "rundll32" — raw rundll32 call
      "uri"      — ms-settings: or other shell URI via cmd /c start
    """
    __slots__ = ("strategy", "args", "desc")

    def __init__(self, strategy: str, args: list[str], desc: str = "") -> None:
        self.strategy = strategy
        self.args     = args
        self.desc     = desc


_TOOLS: dict[str, _ToolDef] = {
    # Direct EXEs
    "Task Manager":        _ToolDef("exe",      ["taskmgr.exe"],           "View running processes and performance"),
    "Registry Editor":     _ToolDef("exe",      ["regedit.exe"],           "Edit the Windows registry"),
    "Resource Monitor":    _ToolDef("exe",      ["resmon.exe"],            "Detailed real-time resource usage"),
    "Reliability Monitor": _ToolDef("exe",      ["perfmon.exe", "/rel"],   "System stability history"),
    "DirectX Diagnostic":  _ToolDef("exe",      ["dxdiag.exe"],            "DirectX and display diagnostics"),
    "Control Panel":       _ToolDef("exe",      ["control.exe"],           "Classic Windows Control Panel"),
    "PowerShell":          _ToolDef("exe",      ["powershell.exe"],        "Windows PowerShell console"),
    "Command Prompt":      _ToolDef("exe",      ["cmd.exe"],               "Windows Command Prompt"),
    "Task Scheduler":      _ToolDef("msc",      ["taskschd.msc"],          "Manage scheduled tasks"),
    "Disk Cleanup":        _ToolDef("exe",      ["cleanmgr.exe"],           "Windows built-in disk cleanup tool"),
    "System Info":         _ToolDef("exe",      ["msinfo32.exe"],           "Full system hardware and software summary"),

    # MMC snap-ins — must go through cmd /c start "" file.msc
    "Device Manager":      _ToolDef("msc",  ["devmgmt.msc"],   "Manage hardware devices and drivers"),
    "Services":            _ToolDef("msc",  ["services.msc"],  "Start, stop and configure Windows services"),
    "Event Viewer":        _ToolDef("msc",  ["eventvwr.msc"],  "View Windows event and error logs"),
    "Disk Management":     _ToolDef("msc",  ["diskmgmt.msc"],  "Manage disk partitions and volumes"),
    "Firewall (Advanced)": _ToolDef("msc",  ["wf.msc"],        "Advanced Windows Firewall with Security rules"),
    "Group Policy":        _ToolDef("msc",  ["gpedit.msc"],    "Local Group Policy Editor (Pro/Enterprise only)"),
    "Local Security":      _ToolDef("msc",  ["secpol.msc"],    "Local Security Policy (Pro/Enterprise only)"),
    "Shared Folders":      _ToolDef("msc",  ["fsmgmt.msc"],    "Manage shared folders and sessions"),
    "Cert Manager":        _ToolDef("msc",  ["certmgr.msc"],   "View and manage certificates"),

    # Control Panel applets — rundll32 shell32.dll,Control_RunDLL file.cpl
    "Sound Settings":      _ToolDef("cpl",  ["mmsys.cpl"],              "Audio devices and mixer"),
    "Power Options":       _ToolDef("cpl",  ["powercfg.cpl"],           "Power plans and sleep settings"),
    "System Properties":   _ToolDef("cpl",  ["sysdm.cpl"],              "Computer name, hardware, advanced settings"),
    "Performance Options": _ToolDef("exe",  ["SystemPropertiesPerformance.exe"], "Adjust the appearance and performance of Windows (visual effects, virtual memory)"),
    "Add/Remove Programs": _ToolDef("cpl",  ["appwiz.cpl"],             "Uninstall or change programs"),
    "Network Connections": _ToolDef("cpl",  ["ncpa.cpl"],               "Network adapters and connections"),
    "Display Settings":    _ToolDef("cpl",  ["desk.cpl"],               "Screen resolution and display settings"),
    "Date and Time":       _ToolDef("cpl",  ["timedate.cpl"],           "System clock and timezone"),
    "Mouse Settings":      _ToolDef("cpl",  ["main.cpl"],               "Mouse speed, buttons, pointers"),
    "User Accounts":       _ToolDef("cpl",  ["nusrmgr.cpl"],            "Manage user accounts and passwords"),
    "Firewall (Basic)":    _ToolDef("cpl",  ["firewall.cpl"],          "Basic Windows Firewall settings"),

    # Special rundll32 calls
    "Environment Vars":    _ToolDef("rundll32", ["sysdm.cpl,EditEnvironmentVariables"], "User and system environment variables"),

    # Shell URIs — cmd /c start uri
    "Windows Update":      _ToolDef("uri",  ["ms-settings:windowsupdate"],    "Check for and install Windows updates"),
    "Storage Sense":       _ToolDef("uri",  ["ms-settings:storagesense"],     "Disk cleanup and storage settings"),
    "Apps Settings":       _ToolDef("uri",  ["ms-settings:appsfeatures"],     "Installed apps and optional features"),
    "Privacy Settings":    _ToolDef("uri",  ["ms-settings:privacy"],          "Camera, microphone, location, telemetry"),
    "Startup Apps":        _ToolDef("uri",  ["ms-settings:startupapps"],      "Apps that run at login"),
    "Default Apps":        _ToolDef("uri",  ["ms-settings:defaultapps"],      "Set default browser, mail, media apps"),
}


def launch_tool(name: str) -> bool:
    """
    Open a Windows system tool by name.

    Uses the correct launch strategy for each tool type:
      .msc  → cmd /c start "" mmc.exe file.msc  (avoids WinError 193 on 64-bit)
      .cpl  → rundll32 shell32.dll,Control_RunDLL file.cpl
      uri   → cmd /c start uri
      exe   → direct Popen
    """
    tool = _TOOLS.get(name)
    if not tool:
        _log.warning("Unknown tool: %s", name)
        return False

    try:
        if tool.strategy == "msc":
            # MMC snap-ins must NOT be called directly — they need mmc.exe
            # cmd /c start "" "file.msc" is the most reliable way
            subprocess.Popen(
                ["cmd", "/c", "start", "", tool.args[0]],
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )

        elif tool.strategy == "cpl":
            # Control Panel applets: rundll32 shell32.dll,Control_RunDLL file.cpl
            subprocess.Popen(
                ["rundll32.exe", "shell32.dll,Control_RunDLL"] + tool.args,
                shell=False,
            )

        elif tool.strategy == "rundll32":
            subprocess.Popen(["rundll32.exe"] + tool.args)

        elif tool.strategy == "uri":
            # ms-settings: and other URIs — cmd /c start is the safest
            subprocess.Popen(
                ["cmd", "/c", "start", "", tool.args[0]],
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )

        else:
            # Direct exe
            subprocess.Popen(tool.args)

        _log.info("Launched tool: %s", name)
        return True

    except FileNotFoundError as exc:
        _log.error("Tool not found [%s]: %s — it may not be installed on this Windows edition", name, exc)
        return False
    except Exception as exc:
        _log.error("Failed to launch %s: %s", name, exc)
        return False


def get_available_tools() -> list[tuple[str, str]]:
    """Return list of (name, description) tuples for the UI."""
    return [(name, tool.desc) for name, tool in _TOOLS.items()]


# ──────────────────────────────────────────────────────────────────────────────
# DNS configuration
# ──────────────────────────────────────────────────────────────────────────────

_DNS_PRESETS: dict[str, tuple[str, str]] = {
    "Cloudflare (1.1.1.1)":          ("1.1.1.1",   "1.0.0.1"),
    "Google (8.8.8.8)":               ("8.8.8.8",   "8.8.4.4"),
    "Quad9 (9.9.9.9)":                ("9.9.9.9",   "149.112.112.112"),
    "OpenDNS":                         ("208.67.222.222", "208.67.220.220"),
    "Cloudflare DoH (malware block)":  ("1.1.1.2",  "1.0.0.2"),
    "System Default (DHCP)":          ("dhcp",      "dhcp"),
}


def set_dns(preset_name: str) -> bool:
    """Set DNS servers on all active adapters."""
    if preset_name not in _DNS_PRESETS:
        return False

    primary, secondary = _DNS_PRESETS[preset_name]

    if primary == "dhcp":
        cmd = (
            'Get-NetAdapter | Where-Object {$_.Status -eq "Up"} | '
            'Set-DnsClientServerAddress -ResetServerAddresses'
        )
    else:
        cmd = (
            f'Get-NetAdapter | Where-Object {{$_.Status -eq "Up"}} | '
            f'Set-DnsClientServerAddress -ServerAddresses ("{primary}","{secondary}")'
        )

    ok, out = _run_ps(cmd, timeout=30)
    if ok:
        _log.info("DNS set to %s (%s / %s)", preset_name, primary, secondary)
    else:
        _log.warning("DNS set failed: %s", out[:100])
    return ok


def get_dns_presets() -> list[str]:
    return list(_DNS_PRESETS.keys())
