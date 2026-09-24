"""
modules/system/info.py

GPU detection uses a multi-source approach, from best to most universal:

  1. pynvml  (NVIDIA Management Library) — most reliable for NVIDIA cards,
              gives usage%, temp, VRAM used/total directly.
              NOT bundled with the driver — pip install pynvml.
  2. OHM WMI — only used if pynvml fails AND OpenHardwareMonitor is running.
  3. Win32_PerfFormattedData_Counters_GPUEngine — Windows' own "GPU Engine"
              perf counters (what Task Manager's GPU graph reads). Works
              for ANY vendor (NVIDIA/AMD/Intel) with no extra software.
  4. Win32_VideoController (WMI) — name/VRAM-total only, no live usage.
  5. None    — if all else fails, cards show N/A (honest)

Sources 3 and 4 both need the `wmi` package — see requirements.txt.
"""

from __future__ import annotations

import platform
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import psutil

# ── pynvml (via the nvidia-ml-py package — see requirements.txt) ────────────
try:
    import warnings as _warnings
    with _warnings.catch_warnings():
        # Defensive: if someone already has the OLD deprecated standalone
        # "pynvml" package installed instead of nvidia-ml-py, it prints a
        # FutureWarning on import that has nothing actionable for this
        # app's users to do about — silence just that one category here.
        _warnings.filterwarnings("ignore", category=FutureWarning)
        import pynvml as _nvml
    _nvml.nvmlInit()
    _NVML_HANDLE = _nvml.nvmlDeviceGetHandleByIndex(0)
    _NVML_OK = True
except Exception:
    _NVML_OK = False
    _NVML_HANDLE = None

# ── WMI — for GPU name / fallback ────────────────────────────────────────────
try:
    import wmi as _wmi
    import pythoncom as _pythoncom
    _WMI_AVAILABLE = True
except ImportError:
    _WMI_AVAILABLE = False

# ── OHM — last resort, only if running ───────────────────────────────────────
_OHM_CHECKED   = False
_OHM_AVAILABLE = False

# Prime CPU counter
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass

# Cache slow values
_cpu_name_cache: Optional[str] = None
_gpu_name_cache: Optional[str] = None


def gpu_packages_missing() -> bool:
    """True if the install button should be offered — i.e. wmi/pynvml
    aren't installed (as opposed to being installed but still not finding
    a working GPU data source, which the button can't fix)."""
    return not _NVML_OK or not _WMI_AVAILABLE


def gpu_detection_status() -> str:
    """
    One-line, actionable explanation for WHY GPU stats might be missing —
    almost always because the optional `wmi`/`pynvml` packages (added to
    requirements.txt so GPU detection works on any vendor) simply aren't
    installed in whatever environment is actually running the app. CPU/RAM/
    Disk only ever needed psutil, so if those work but GPU doesn't, this is
    the first thing to check — not a bug in the detection logic itself.
    """
    missing = []
    if not _NVML_OK:
        missing.append("nvidia-ml-py")
    if not _WMI_AVAILABLE:
        missing.append("wmi")
    if missing:
        return f"Missing package(s): {', '.join(missing)} — run: pip install {' '.join(missing)}"
    return "Packages present but no GPU data source responded — see logs"


def install_gpu_packages() -> tuple[bool, str]:
    """
    Installs the missing wmi/pynvml packages into the SAME Python
    interpreter running this app (sys.executable -m pip ...), so it
    reliably targets the right environment regardless of how the app
    itself was launched.

    Note: this module already imported (or failed to import) wmi/pynvml
    at process startup, and _NVML_OK/_WMI_AVAILABLE were decided then —
    installing the packages now does NOT retroactively fix an already-
    running process. The app needs a restart afterward for GPU detection
    to actually pick them up.
    """
    import subprocess
    import sys

    missing = []
    if not _NVML_OK:
        missing.append("nvidia-ml-py")
    if not _WMI_AVAILABLE:
        missing.append("wmi")
    if not missing:
        return True, "GPU packages are already installed."

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *missing],
            capture_output=True, text=True, timeout=120,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            return True, f"Installed {', '.join(missing)}. Restart DWUT for GPU stats to appear."
        return False, out[-800:] if out else "pip install failed (no output captured)."
    except Exception as exc:
        return False, str(exc)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpuStats:
    name: str
    usage_pct: Optional[float]
    temp_c: Optional[float]
    vram_used_mb: Optional[float]
    vram_total_mb: Optional[float]
    source: str = ""     # "nvml" | "ohm" | "wmi"


@dataclass(frozen=True)
class NetworkIO:
    bytes_sent: int
    bytes_recv: int
    send_rate_kbps: Optional[float]
    recv_rate_kbps: Optional[float]


@dataclass(frozen=True)
class DashboardSnapshot:
    cpu_pct: float
    cpu_freq_mhz: Optional[float]
    cpu_core_count: int
    cpu_name: str
    ram_pct: float
    ram_used_gb: float
    ram_total_gb: float
    disk_pct: float
    disk_free_gb: float
    disk_total_gb: float
    gpu: Optional[GpuStats]
    net: NetworkIO
    uptime: timedelta
    is_admin: bool
    windows_version: str
    hostname: str
    pending_reboot: bool
    captured_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class SystemHealthScore:
    total: int
    cpu: int
    ram: int
    disk: int
    uptime: int
    label: str


# ── Internal state ────────────────────────────────────────────────────────────

_last_net_io: Optional[object] = None
_last_net_time: Optional[float] = None


# ── CPU ───────────────────────────────────────────────────────────────────────

def _get_cpu_name() -> str:
    global _cpu_name_cache
    if _cpu_name_cache:
        return _cpu_name_cache
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        _cpu_name_cache = name.strip()
    except Exception:
        _cpu_name_cache = platform.processor() or "Unknown CPU"
    return _cpu_name_cache


# ── GPU — three-source approach ───────────────────────────────────────────────

def _gpu_via_nvml() -> Optional[GpuStats]:
    """
    NVIDIA Management Library — works on any NVIDIA GPU (GTX/RTX) without OHM.
    pynvml is bundled with the NVIDIA driver or installable via: pip install pynvml
    """
    if not _NVML_OK or _NVML_HANDLE is None:
        return None
    try:
        import pynvml as _nvml
        name_bytes = _nvml.nvmlDeviceGetName(_NVML_HANDLE)
        name = name_bytes.decode() if isinstance(name_bytes, bytes) else name_bytes

        util  = _nvml.nvmlDeviceGetUtilizationRates(_NVML_HANDLE)
        usage = float(util.gpu)

        try:
            temp = float(_nvml.nvmlDeviceGetTemperature(
                _NVML_HANDLE, _nvml.NVML_TEMPERATURE_GPU
            ))
        except Exception:
            temp = None

        try:
            mem = _nvml.nvmlDeviceGetMemoryInfo(_NVML_HANDLE)
            vram_used  = mem.used  / (1024 * 1024)
            vram_total = mem.total / (1024 * 1024)
        except Exception:
            vram_used = vram_total = None

        return GpuStats(
            name=name, usage_pct=usage, temp_c=temp,
            vram_used_mb=vram_used, vram_total_mb=vram_total,
            source="nvml",
        )
    except Exception:
        return None


def _gpu_via_wmi_name() -> str:
    """Get GPU name from Win32_VideoController — works for any GPU."""
    global _gpu_name_cache
    if _gpu_name_cache:
        return _gpu_name_cache
    if not _WMI_AVAILABLE:
        return "GPU"
    try:
        _pythoncom.CoInitialize()
        c = _wmi.WMI()
        for card in c.Win32_VideoController():
            n = getattr(card, "Name", "") or ""
            if n and "Microsoft" not in n and "Remote" not in n:
                _gpu_name_cache = n
                return n
    except Exception:
        pass
    finally:
        try:
            _pythoncom.CoUninitialize()
        except Exception:
            pass
    _gpu_name_cache = "GPU"
    return "GPU"


def _check_ohm() -> bool:
    global _OHM_CHECKED, _OHM_AVAILABLE
    if _OHM_CHECKED:
        return _OHM_AVAILABLE
    _OHM_CHECKED = True
    if not _WMI_AVAILABLE:
        return False
    try:
        _pythoncom.CoInitialize()
        c = _wmi.WMI(namespace=r"root\OpenHardwareMonitor")
        list(c.Sensor())[:1]
        _OHM_AVAILABLE = True
    except Exception:
        _OHM_AVAILABLE = False
    finally:
        try:
            _pythoncom.CoUninitialize()
        except Exception:
            pass
    return _OHM_AVAILABLE


def _gpu_via_ohm() -> Optional[GpuStats]:
    if not _check_ohm():
        return None
    try:
        _pythoncom.CoInitialize()
        c = _wmi.WMI(namespace=r"root\OpenHardwareMonitor")
        usage = temp = vram_used = vram_total = None
        name = "GPU"
        for s in c.Sensor():
            sn = s.Name.upper()
            st = s.SensorType
            if st == "Load"        and "GPU" in sn and "CORE" in sn: usage      = float(s.Value)
            elif st == "Temperature" and "GPU" in sn:                  temp       = float(s.Value)
            elif st == "SmallData"  and "GPU" in sn:
                if   "USED"  in sn: vram_used  = float(s.Value)
                elif "TOTAL" in sn: vram_total = float(s.Value)
        if usage is None:
            return None
        return GpuStats(name=name, usage_pct=usage, temp_c=temp,
                        vram_used_mb=vram_used, vram_total_mb=vram_total, source="ohm")
    except Exception:
        return None
    finally:
        try:
            _pythoncom.CoUninitialize()
        except Exception:
            pass


def _gpu_via_perfcounters() -> Optional[GpuStats]:
    """
    Windows' native "GPU Engine" performance counters — the same data
    source Task Manager's GPU graph reads from. Works for ANY vendor
    (NVIDIA/AMD/Intel) with zero extra drivers or background apps,
    unlike NVML (NVIDIA-only) and OHM (needs a separate app running).
    Only needs the `wmi` package, which is already used for the GPU name.
    """
    if not _WMI_AVAILABLE:
        return None
    try:
        _pythoncom.CoInitialize()
        c = _wmi.WMI()
        rows = c.Win32_PerfFormattedData_Counters_GPUEngine()

        # Each physical GPU exposes several "engines" (3D, Copy, VideoDecode,
        # etc.) as separate counter instances. Task Manager reports the busiest
        # engine per adapter as "GPU usage" — mirror that instead of summing,
        # which would double-count and can read >100%.
        best_by_adapter: dict[str, float] = {}
        for row in rows:
            name = getattr(row, "Name", "") or ""
            util = getattr(row, "UtilizationPercentage", None)
            if util is None:
                continue
            adapter_key = name.split("engtype_")[0]
            util = float(util)
            if util > best_by_adapter.get(adapter_key, -1.0):
                best_by_adapter[adapter_key] = util

        if not best_by_adapter:
            return None

        usage = max(best_by_adapter.values())
        return GpuStats(
            name=_gpu_via_wmi_name(), usage_pct=usage, temp_c=None,
            vram_used_mb=None, vram_total_mb=None, source="perfcounter",
        )
    except Exception:
        return None
    finally:
        try:
            _pythoncom.CoUninitialize()
        except Exception:
            pass


def _get_gpu_stats() -> Optional[GpuStats]:
    """Try NVML first (best for NVIDIA), then OHM, then the native perf
    counters (works for any vendor), then fall back to a name-only stub."""
    # 1. pynvml — works on RTX 4070, GTX series, no OHM needed
    stats = _gpu_via_nvml()
    if stats:
        return stats

    # 2. OHM WMI — works for AMD too if OHM is running
    stats = _gpu_via_ohm()
    if stats:
        return stats

    # 3. Native "GPU Engine" perf counters — works for any vendor, no
    #    extra software required (this is what Task Manager itself uses)
    stats = _gpu_via_perfcounters()
    if stats:
        return stats

    # 4. At least show GPU name from WMI even if no usage data
    name = _gpu_via_wmi_name()
    if name and name != "GPU":
        return GpuStats(
            name=name, usage_pct=None, temp_c=None,
            vram_used_mb=None, vram_total_mb=None, source="wmi_name",
        )
    return None


# ── Network ───────────────────────────────────────────────────────────────────

def _get_net_io() -> NetworkIO:
    global _last_net_io, _last_net_time
    now = time.monotonic()
    current = psutil.net_io_counters()
    send_rate = recv_rate = None
    if _last_net_io is not None and _last_net_time is not None:
        elapsed = now - _last_net_time
        if elapsed > 0:
            send_rate = (current.bytes_sent - _last_net_io.bytes_sent) / elapsed / 1024
            recv_rate = (current.bytes_recv - _last_net_io.bytes_recv) / elapsed / 1024
    _last_net_io   = current
    _last_net_time = now
    return NetworkIO(current.bytes_sent, current.bytes_recv, send_rate, recv_rate)


# ── Pending reboot ────────────────────────────────────────────────────────────

def _pending_reboot() -> bool:
    try:
        import winreg
        for hive, path in [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
        ]:
            try:
                k = winreg.OpenKey(hive, path); winreg.CloseKey(k); return True
            except Exception:
                pass
    except Exception:
        pass
    return False


# ── Main snapshot ─────────────────────────────────────────────────────────────

def get_dashboard_snapshot() -> DashboardSnapshot:
    from app.permissions import is_admin
    cpu_pct = psutil.cpu_percent(interval=None)
    freq    = psutil.cpu_freq()
    mem     = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\")
        disk_pct, disk_free, disk_total = disk.percent, disk.free/(1024**3), disk.total/(1024**3)
    except Exception:
        disk_pct = disk_free = disk_total = 0.0

    return DashboardSnapshot(
        cpu_pct=cpu_pct,
        cpu_freq_mhz=freq.current if freq else None,
        cpu_core_count=psutil.cpu_count(logical=True) or 0,
        cpu_name=_get_cpu_name(),
        ram_pct=mem.percent,
        ram_used_gb=mem.used/(1024**3),
        ram_total_gb=mem.total/(1024**3),
        disk_pct=disk_pct,
        disk_free_gb=disk_free,
        disk_total_gb=disk_total,
        gpu=_get_gpu_stats(),
        net=_get_net_io(),
        uptime=datetime.now() - datetime.fromtimestamp(psutil.boot_time()),
        is_admin=is_admin(),
        windows_version=platform.version(),
        hostname=socket.gethostname(),
        pending_reboot=_pending_reboot(),
    )


def compute_health_score(snap: DashboardSnapshot) -> SystemHealthScore:
    cpu_s  = int(max(0, (100 - snap.cpu_pct)  / 100 * 30))
    ram_s  = int(max(0, (100 - snap.ram_pct)  / 100 * 30))
    disk_s = int(min(25, (snap.disk_free_gb / max(snap.disk_total_gb, 1)) * 100 / 2)) if snap.disk_total_gb else 0
    days   = snap.uptime.days
    up_s   = 15 if days < 7 else 12 if days < 14 else 8 if days < 30 else 4 if days < 60 else 0
    total  = min(100, cpu_s + ram_s + disk_s + up_s)
    if snap.pending_reboot:
        total = max(0, total - 10)
    label = "Excellent" if total >= 85 else "Good" if total >= 70 else "Fair" if total >= 50 else "Poor"
    return SystemHealthScore(total=total, cpu=cpu_s, ram=ram_s, disk=disk_s, uptime=up_s, label=label)
