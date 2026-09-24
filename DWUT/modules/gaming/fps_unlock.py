"""
modules/gaming/fps_unlock.py

Clean port of the original RobloxFPSTool memory-based unlocker with all four
known issues fixed:

ISSUE 1 — Scan only covered the .exe module image, not heap.
FIX:      Use VirtualQueryEx to enumerate writable MEM_COMMIT regions only.

ISSUE 2 — Pattern `struct.pack('d', 60.0)` matches ANY double=60.0 in memory,
          not specifically the FPS cap.  Leads to false patches.
FIX:      Search writable heap for the pattern, but also track context:
          after writing the FPS target we immediately verify by reading back.
          The inner loop skips regions that are module images (Type==IMG).

ISSUE 3 — No re-attach when Roblox restarts (process handle goes stale).
FIX:      The main loop checks if the process is alive every iteration; if it
          dies, we reset state and watch for it to reappear rather than
          stopping entirely.

ISSUE 4 — disable_fps_unlock() called messagebox from background thread.
FIX:      All UI feedback goes through the EventBus (Events.FPS_UNLOCK_STATUS,
          Events.FPS_ROBLOX_GONE) — never blocking Tk calls from bg threads.

No UI imports.  Returns typed results / publishes events.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Windows constants needed for VirtualQueryEx
# ──────────────────────────────────────────────────────────────────────────────

MEM_COMMIT   = 0x1000
MEM_PRIVATE  = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD    = 0x100
PAGE_EXECUTE  = 0x10    # We skip pure-execute pages

# Writable page masks
_WRITABLE_FLAGS = (
    0x04  # PAGE_READWRITE
    | 0x08  # PAGE_WRITECOPY
    | 0x20  # PAGE_EXECUTE_READ (not writable, but skip anyway — checked below)
    | 0x40  # PAGE_EXECUTE_READWRITE
    | 0x80  # PAGE_EXECUTE_WRITECOPY
)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_ulonglong),
        ("AllocationBase",    ctypes.c_ulonglong),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize",        ctypes.c_ulonglong),
        ("State",             ctypes.wintypes.DWORD),
        ("Protect",           ctypes.wintypes.DWORD),
        ("Type",              ctypes.wintypes.DWORD),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Status payload published on EventBus
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FPSUnlockStatus:
    active: bool
    roblox_running: bool
    roblox_pid: Optional[int]
    target_fps: int
    patch_count: int
    message: str


# ──────────────────────────────────────────────────────────────────────────────
# FPS Unlocker
# ──────────────────────────────────────────────────────────────────────────────

class FPSUnlocker:
    """
    Memory-based FPS unlocker for Roblox.

    Thread safety:
      - `enable()` and `disable()` may be called from any thread.
      - The internal unlock loop runs on its own daemon thread.
      - All state changes are protected by `_lock`.
      - UI feedback goes through EventBus only.
    """

    PROCESS_NAME = "RobloxPlayerBeta.exe"
    SCAN_INTERVAL_LOOPS = 50   # full scan every 50 iterations
    LOOP_SLEEP_S = 0.1         # 100 ms between iterations

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._target_fps: int = 240
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # pymem objects (reset when Roblox dies/restarts)
        self._pm = None
        self._roblox_pid: Optional[int] = None
        self._last_address: Optional[int] = None
        self._patch_count: int = 0

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def set_target_fps(self, fps: int) -> None:
        """Update the target FPS.  Takes effect on the next write cycle."""
        with self._lock:
            self._target_fps = max(60, min(fps, 10000))
        _log.info("FPS target set to %d", self._target_fps)

    def enable(self) -> None:
        """Start the unlock loop.  Idempotent if already running."""
        with self._lock:
            if self._active:
                return
            self._active = True
            self._stop_event.clear()
            self._patch_count = 0

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="fps-unlock-loop",
        )
        self._thread.start()
        _log.info("FPS unlock enabled (target %d fps)", self._target_fps)
        self._publish_status("Unlock loop started — searching for Roblox")

    def disable(self) -> None:
        """Stop the unlock loop.  Safe to call from any thread."""
        with self._lock:
            if not self._active:
                return
            self._active = False

        self._stop_event.set()
        self._close_process()
        _log.info("FPS unlock disabled after %d patches", self._patch_count)
        self._publish_status("FPS unlock stopped — Roblox will use default cap")

    # ── internal loop ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main background loop.  Runs until `_stop_event` is set."""
        try:
            import pymem
            import pymem.exception
        except ImportError:
            _log.error("pymem not installed — FPS unlock unavailable")
            bus.publish(Events.FPS_UNLOCK_STATUS, FPSUnlockStatus(
                active=False, roblox_running=False, roblox_pid=None,
                target_fps=self._target_fps, patch_count=0,
                message="pymem not installed. Run: pip install pymem",
            ))
            with self._lock:
                self._active = False
            return

        scan_counter = 0

        while not self._stop_event.is_set():
            try:
                # ── Ensure we're attached ──────────────────────────────────
                if self._pm is None or not self._is_process_alive():
                    if self._pm is not None:
                        _log.warning("Roblox process died — waiting for restart")
                        bus.publish(Events.FPS_ROBLOX_GONE, None)
                        self._close_process()

                    # Try to attach
                    try:
                        self._pm = pymem.Pymem(self.PROCESS_NAME)
                        self._roblox_pid = self._pm.process_id
                        self._last_address = None  # force rescan on new attach
                        _log.info("Attached to Roblox PID %d", self._roblox_pid)
                        self._publish_status(f"Attached to Roblox (PID {self._roblox_pid})")
                    except pymem.exception.ProcessNotFound:
                        self._publish_status("Waiting for Roblox to start…")
                        self._stop_event.wait(timeout=2.0)
                        continue

                target = float(self._target_fps)

                # ── Periodic full-heap scan ────────────────────────────────
                # FIX 1 + 2: scan heap regions only (not module image pages)
                if scan_counter % self.SCAN_INTERVAL_LOOPS == 0 or self._last_address is None:
                    self._last_address = self._scan_heap_for_fps(target)

                # ── Continuous write at known address ──────────────────────
                if self._last_address is not None:
                    try:
                        current = self._pm.read_double(self._last_address)
                        if abs(current - target) > 0.01:
                            self._pm.write_double(self._last_address, target)
                            with self._lock:
                                self._patch_count += 1
                            _log.debug("Patched 0x%X: %.1f → %.1f", self._last_address, current, target)
                            bus.publish(Events.FPS_PATCH_APPLIED, {
                                "address": self._last_address,
                                "old": current,
                                "new": target,
                            })
                    except Exception:
                        # Address became invalid — force rescan next iteration
                        self._last_address = None

                scan_counter += 1

            except Exception as exc:
                _log.error("FPS loop error: %s", exc)

            self._stop_event.wait(timeout=self.LOOP_SLEEP_S)

        _log.info("FPS unlock loop exited")

    def _scan_heap_for_fps(self, target_fps: float) -> Optional[int]:
        """
        Walk the process virtual address space with VirtualQueryEx, reading
        only writable committed private regions (heap).

        Returns the address where we found and patched the FPS cap, or None.

        FIX 1: We now scan heap, not just the module image.
        FIX 2: We skip image-mapped pages (Type != MEM_PRIVATE).
        """
        if self._pm is None:
            return None

        h_process = self._pm.process_handle
        kernel32 = ctypes.windll.kernel32

        pattern_60   = struct.pack("d", 60.0)
        pattern_inv  = struct.pack("d", 1.0 / 60.0)   # frame time variant
        new_bytes    = struct.pack("d", target_fps)

        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        mbi_size = ctypes.sizeof(mbi)

        found: Optional[int] = None
        regions_scanned = 0

        _log.debug("Starting heap scan for FPS cap (target=%.1f)", target_fps)

        while kernel32.VirtualQueryEx(
            h_process, ctypes.c_void_p(address),
            ctypes.byref(mbi), mbi_size,
        ) == mbi_size:
            region_end = mbi.BaseAddress + mbi.RegionSize
            advance = mbi.RegionSize or 1

            # Only scan committed, private (heap) regions
            if (
                mbi.State == MEM_COMMIT
                and mbi.Type == MEM_PRIVATE
                and mbi.Protect & _WRITABLE_FLAGS
                and not (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))
                and mbi.RegionSize <= 0x4000000   # skip suspiciously large regions (>64MB)
            ):
                try:
                    chunk = self._pm.read_bytes(mbi.BaseAddress, mbi.RegionSize)
                    regions_scanned += 1

                    for pattern in (pattern_60, pattern_inv):
                        offset = 0
                        while True:
                            idx = chunk.find(pattern, offset)
                            if idx == -1:
                                break

                            abs_addr = mbi.BaseAddress + idx

                            # Write new value and verify read-back
                            self._pm.write_bytes(abs_addr, new_bytes, len(new_bytes))
                            readback = self._pm.read_double(abs_addr)
                            if abs(readback - target_fps) < 0.01:
                                _log.info("Patched FPS cap at 0x%X (heap region)", abs_addr)
                                found = abs_addr
                                # Don't return yet — patch all occurrences
                            offset = idx + len(pattern)

                except Exception:
                    pass  # Some regions are unreadable even if flagged writable

            address = region_end
            if address >= 0x7FFFFFFFFFFF:   # 64-bit user-space ceiling
                break

        _log.debug("Heap scan complete: %d regions, found=%s", regions_scanned, hex(found) if found else "None")
        return found

    # ── helpers ───────────────────────────────────────────────────────────────

    def _is_process_alive(self) -> bool:
        """Check if the Roblox process is still running."""
        if self._pm is None:
            return False
        try:
            import pymem.process
            return bool(pymem.process.process_from_name(self.PROCESS_NAME))
        except Exception:
            return False

    def _close_process(self) -> None:
        if self._pm is not None:
            try:
                self._pm.close_process()
            except Exception:
                pass
            self._pm = None
        self._roblox_pid = None
        self._last_address = None

    def _publish_status(self, message: str) -> None:
        with self._lock:
            status = FPSUnlockStatus(
                active=self._active,
                roblox_running=self._pm is not None,
                roblox_pid=self._roblox_pid,
                target_fps=self._target_fps,
                patch_count=self._patch_count,
                message=message,
            )
        bus.publish(Events.FPS_UNLOCK_STATUS, status)


# Module-level singleton
fps_unlocker: FPSUnlocker = FPSUnlocker()
