"""
modules/system/power_modes.py

Two persistent, toggleable power modes on top of Windows' own power plans:

  Ultimate Performance — unhides and activates Windows' own built-in
  "Ultimate Performance" plan (it exists on every Windows 10/11 install
  but is hidden from the power plan list unless duplicated out). Removes
  the small background throttling/idle timers Windows normally applies.
  A safe pick for a desktop PC or a laptop that's staying plugged in.

  Extreme Performance — takes Ultimate Performance further: also disables
  USB selective suspend, PCI Express Link State Power Management (ASPM),
  and pins the CPU minimum state to 100% so cores never downclock. This
  uses noticeably more power and runs hotter — it's built for a desktop
  (or a laptop that's plugged in and not moving), not for squeezing
  battery life out of a laptop on the go.

Both are OFF by default and turning either off reverts to Windows'
Balanced plan — not whatever custom plan may have been active before,
since Windows doesn't track that for us.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from core.logger import get_logger

_log = get_logger(__name__)

_PLAN_ULTIMATE_PERF = "e9a42b02-d5df-448d-aa00-03f14749eb61"
_PLAN_BALANCED       = "381b4222-f694-41f0-9685-ff5bb260df2e"


def _run(args: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return False, str(exc)


def _find_scheme_guid(name_fragment: str) -> Optional[str]:
    """Parse `powercfg /list` for a scheme whose friendly name contains
    name_fragment. Lines look like:
    'Power Scheme GUID: xxxxxxxx-xxxx-...  (Ultimate Performance)'"""
    ok, out = _run(["powercfg", "/list"])
    if not ok:
        return None
    for line in out.splitlines():
        if name_fragment.lower() in line.lower():
            for part in line.split():
                if len(part) == 36 and part.count("-") == 4:
                    return part
    return None


def _ensure_ultimate_scheme() -> Optional[str]:
    """Return the Ultimate Performance scheme GUID, duplicating the hidden
    built-in plan into the visible list first if it isn't there yet."""
    existing = _find_scheme_guid("Ultimate Performance")
    if existing:
        return existing
    ok, out = _run(["powercfg", "/duplicatescheme", _PLAN_ULTIMATE_PERF])
    if not ok:
        return None
    for part in out.split():
        if len(part) == 36 and part.count("-") == 4:
            return part
    return _find_scheme_guid("Ultimate Performance")


def _ensure_extreme_scheme() -> Optional[str]:
    """
    Duplicate Ultimate Performance again under its own name so its extra
    tweaks (USB/PCIe power-saving off, CPU pinned to 100% min state) can
    live on a separate plan from plain Ultimate Performance.
    """
    existing = _find_scheme_guid("Extreme Performance")
    if existing:
        return existing

    base = _ensure_ultimate_scheme()
    if not base:
        return None

    ok, out = _run(["powercfg", "/duplicatescheme", base])
    if not ok:
        return None
    new_guid = None
    for part in out.split():
        if len(part) == 36 and part.count("-") == 4:
            new_guid = part
            break
    if not new_guid:
        return None

    _run(["powercfg", "/changename", new_guid, "Extreme Performance",
          "Maximum performance -- disables USB/PCIe power saving and pins CPU to 100% min state."])

    # CPU minimum state -> 100% (cores never downclock)
    _run(["powercfg", "/setacvalueindex", new_guid, "SUB_PROCESSOR", "PROCTHROTTLEMIN", "100"])
    _run(["powercfg", "/setdcvalueindex", new_guid, "SUB_PROCESSOR", "PROCTHROTTLEMIN", "100"])
    # USB selective suspend off
    _run(["powercfg", "/setacvalueindex", new_guid, "SUB_USB", "USBSELECTSUSPEND", "0"])
    _run(["powercfg", "/setdcvalueindex", new_guid, "SUB_USB", "USBSELECTSUSPEND", "0"])
    # PCI Express Link State Power Management (ASPM) off
    _run(["powercfg", "/setacvalueindex", new_guid, "SUB_PCIEXPRESS", "ASPM", "0"])
    _run(["powercfg", "/setdcvalueindex", new_guid, "SUB_PCIEXPRESS", "ASPM", "0"])

    return new_guid


def is_ultimate_performance_active() -> bool:
    ok, out = _run(["powercfg", "/getactivescheme"])
    return ok and "ultimate performance" in out.lower()


def is_extreme_performance_active() -> bool:
    ok, out = _run(["powercfg", "/getactivescheme"])
    return ok and "extreme performance" in out.lower()


def enable_ultimate_performance() -> tuple[bool, str]:
    guid = _ensure_ultimate_scheme()
    if not guid:
        return False, "Could not create the Ultimate Performance power plan."
    ok, out = _run(["powercfg", "/setactive", guid])
    if ok:
        _log.info("Activated Ultimate Performance power plan")
        return True, "Ultimate Performance is now active."
    return False, out or "Failed to activate Ultimate Performance."


def enable_extreme_performance() -> tuple[bool, str]:
    guid = _ensure_extreme_scheme()
    if not guid:
        return False, "Could not create the Extreme Performance power plan."
    ok, out = _run(["powercfg", "/setactive", guid])
    if ok:
        _log.info("Activated Extreme Performance power plan")
        return True, "Extreme Performance is now active."
    return False, out or "Failed to activate Extreme Performance."


def revert_to_balanced() -> tuple[bool, str]:
    ok, out = _run(["powercfg", "/setactive", _PLAN_BALANCED])
    if ok:
        _log.info("Reverted to Balanced power plan")
        return True, "Reverted to the Balanced power plan."
    return False, out or "Failed to switch power plan."
