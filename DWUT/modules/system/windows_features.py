"""
modules/system/windows_features.py

Windows optional features — installed/removed via DISM, distinct from the
registry-tweak FeatureDef system in modules/debloat/operations.py. These
take longer (DISM can take anywhere from a few seconds to a couple of
minutes) and some require a restart to finish.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)


@dataclass
class WinFeatureResult:
    tool: str
    success: bool
    output: str
    duration_s: float


@dataclass
class WinFeatureDef:
    key: str
    name: str
    desc: str
    dism_names: list[str]          # one or more /FeatureName: values
    recommended: bool = False
    needs_restart: bool = True


WINDOWS_FEATURES: list[WinFeatureDef] = [
    WinFeatureDef(
        key="dotnet35", name=".NET Framework (Versions 2, 3, 4)",
        desc="Older .NET Framework 3.5 (includes 2.0/3.0) — required by some legacy apps and games",
        dism_names=["NetFx3"],
    ),
    WinFeatureDef(
        key="hyperv", name="Hyper-V",
        desc="Windows' built-in virtualization platform for running VMs (Pro/Enterprise editions only)",
        dism_names=["Microsoft-Hyper-V-All"],
    ),
    WinFeatureDef(
        key="legacy_media", name="Legacy Media Components (WMP, DirectPlay)",
        desc="Windows Media Player and DirectPlay — needed by some older games and media apps",
        dism_names=["WindowsMediaPlayer", "DirectPlay"],
    ),
    WinFeatureDef(
        key="nfs", name="Network File System (NFS)",
        desc="Client for mounting NFS shares (common on Linux/NAS network storage)",
        dism_names=["ServicesForNFS-ClientOnly", "ClientForNFS-Infrastructure", "NFS-Administration"],
    ),
    WinFeatureDef(
        key="sandbox", name="Windows Sandbox",
        desc="Lightweight disposable VM for safely testing untrusted software (Pro/Enterprise only)",
        dism_names=["Containers-DisposableClientVM"],
    ),
    WinFeatureDef(
        key="wsl", name="Windows Subsystem for Linux (WSL)",
        desc="Run a real Linux environment alongside Windows. Also enables the Virtual Machine Platform "
             "needed for WSL2 — after this, run 'wsl --install' to get a distro.",
        dism_names=["Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform"],
    ),
]


def _run(cmd: list[str], timeout: int = 600) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


def enable_windows_feature(feat: WinFeatureDef) -> WinFeatureResult:
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, f"Enabling {feat.name}…")
    outputs = []
    ok_all = True
    for dism_name in feat.dism_names:
        ok, out = _run([
            "Dism", "/Online", "/Enable-Feature",
            f"/FeatureName:{dism_name}", "/All", "/NoRestart",
        ])
        outputs.append(f"[{dism_name}] {out.strip()}")
        ok_all = ok_all and ok
    result = WinFeatureResult(
        tool=feat.name, success=ok_all,
        output="\n".join(outputs), duration_s=time.monotonic() - t0,
    )
    bus.publish(Events.REPAIR_DONE, result)
    return result


def disable_windows_feature(feat: WinFeatureDef) -> WinFeatureResult:
    t0 = time.monotonic()
    bus.publish(Events.REPAIR_PROGRESS, f"Disabling {feat.name}…")
    outputs = []
    ok_all = True
    for dism_name in feat.dism_names:
        ok, out = _run([
            "Dism", "/Online", "/Disable-Feature",
            f"/FeatureName:{dism_name}", "/NoRestart",
        ])
        outputs.append(f"[{dism_name}] {out.strip()}")
        ok_all = ok_all and ok
    result = WinFeatureResult(
        tool=feat.name, success=ok_all,
        output="\n".join(outputs), duration_s=time.monotonic() - t0,
    )
    bus.publish(Events.REPAIR_DONE, result)
    return result


def enable_features_batch(keys: list[str]) -> list[WinFeatureResult]:
    """Enable several features in one go — used by the 'Install Features' button."""
    by_key = {f.key: f for f in WINDOWS_FEATURES}
    results = []
    for key in keys:
        feat = by_key.get(key)
        if feat:
            results.append(enable_windows_feature(feat))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# The remaining two "Features" screenshot items — Legacy F8 Boot Recovery and
# the Registry Backup scheduled task — aren't DISM features, they're a boot
# config flag and a scheduled task respectively.
# ──────────────────────────────────────────────────────────────────────────────

def enable_legacy_f8_recovery() -> WinFeatureResult:
    """bcdedit: show the legacy F8 'Advanced Boot Options' menu instead of the modern recovery UI."""
    t0 = time.monotonic()
    ok, out = _run(["bcdedit", "/set", "{current}", "bootmenupolicy", "Legacy"])
    return WinFeatureResult(tool="Legacy F8 Boot Recovery — Enable", success=ok, output=out, duration_s=time.monotonic() - t0)


def disable_legacy_f8_recovery() -> WinFeatureResult:
    """bcdedit: restore the modern (Windows 8+) boot recovery menu."""
    t0 = time.monotonic()
    ok, out = _run(["bcdedit", "/set", "{current}", "bootmenupolicy", "Standard"])
    return WinFeatureResult(tool="Legacy F8 Boot Recovery — Disable", success=ok, output=out, duration_s=time.monotonic() - t0)


def enable_registry_backup_task() -> WinFeatureResult:
    """Re-enables the built-in daily registry backup scheduled task, which
    Microsoft disabled by default starting Windows 10 1803."""
    t0 = time.monotonic()
    ok, out = _run([
        "schtasks", "/Change", "/TN", r"Microsoft\Windows\Registry\RegIdleBackup", "/Enable",
    ])
    return WinFeatureResult(tool="Registry Backup Task", success=ok, output=out, duration_s=time.monotonic() - t0)
