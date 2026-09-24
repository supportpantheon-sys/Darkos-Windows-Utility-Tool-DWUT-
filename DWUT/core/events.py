"""
EventBus — lightweight publish/subscribe for cross-module communication.

Background workers (modules/) post events here.
UI pages subscribe to the events they care about.

RULE: background threads NEVER call .configure() on widgets directly.
They post an event.  The UI page receives it on the main thread via
the Tk `after` queue and updates its own widgets.

Usage
─────
    # Subscribe (call from UI setup, on the main thread)
    bus.subscribe("dashboard.stats", self._on_stats_update)

    # Publish (call from any thread)
    bus.publish("dashboard.stats", snapshot)

    # The callback fires on the main thread (via `after(0, ...)`)
    def _on_stats_update(self, snapshot: DashboardSnapshot) -> None:
        self.cpu_label.configure(text=f"{snapshot.cpu_pct:.0f}%")
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable, Optional


class EventBus:
    """
    Thread-safe pub/sub bus.

    Subscribers are called on the main Tk thread if a root window has been
    registered via `set_root(root)`.  Without a root window the callbacks are
    called on the publishing thread — only use that for non-UI subscribers
    (e.g. loggers, state writers).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._root: Optional[Any] = None   # Tk root window

    def set_root(self, root: Any) -> None:
        """Register the Tk root so callbacks can be dispatched via after(0,…)."""
        self._root = root

    # ── subscribe / unsubscribe ───────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        """Register `callback` for `event`.  Safe to call from main thread."""
        with self._lock:
            if callback not in self._subscribers[event]:
                self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Remove `callback` from `event`."""
        with self._lock:
            try:
                self._subscribers[event].remove(callback)
            except ValueError:
                pass

    def unsubscribe_all(self, callback: Callable) -> None:
        """Remove `callback` from every event it was subscribed to."""
        with self._lock:
            for lst in self._subscribers.values():
                try:
                    lst.remove(callback)
                except ValueError:
                    pass

    # ── publish ───────────────────────────────────────────────────────────────

    def publish(self, event: str, data: Any = None) -> None:
        """
        Fire all callbacks registered for `event`, passing `data`.

        If a Tk root has been registered, each callback is scheduled via
        `root.after(0, cb, data)` so it runs safely on the main thread.
        Otherwise callbacks are invoked on the calling thread.
        """
        with self._lock:
            callbacks = list(self._subscribers.get(event, []))

        for cb in callbacks:
            try:
                if self._root is not None:
                    self._root.after(0, cb, data)
                else:
                    cb(data)
            except Exception as exc:
                # Never let a bad subscriber kill the publisher
                print(f"[EventBus] Error in subscriber for '{event}': {exc}")

    def publish_many(self, events: dict[str, Any]) -> None:
        """Publish multiple events at once.  Useful for batch state updates."""
        for event, data in events.items():
            self.publish(event, data)


# ── Well-known event names ────────────────────────────────────────────────────
# Import these constants rather than using raw strings to avoid typos.

class Events:
    # Dashboard
    DASHBOARD_STATS       = "dashboard.stats"
    DASHBOARD_HEALTH      = "dashboard.health"

    # System
    OPTIMIZER_PROGRESS    = "optimizer.progress"
    OPTIMIZER_DONE        = "optimizer.done"
    REPAIR_PROGRESS       = "repair.progress"
    REPAIR_DONE           = "repair.done"
    STARTUP_LOADED        = "startup.loaded"
    STARTUP_CHANGED       = "startup.changed"

    # Gaming
    GAME_BOOST_CHANGED    = "gaming.boost_changed"
    FPS_UNLOCK_STATUS     = "gaming.fps_status"
    FPS_PATCH_APPLIED     = "gaming.fps_patched"
    FPS_ROBLOX_GONE       = "gaming.roblox_gone"

    # Debloat
    DEBLOAT_PROGRESS      = "debloat.progress"
    DEBLOAT_DONE          = "debloat.done"

    # Proxy
    PROXY_FETCH_PROGRESS  = "proxy.fetch_progress"
    PROXY_FETCH_DONE      = "proxy.fetch_done"
    PROXY_CHECK_PROGRESS  = "proxy.check_progress"
    PROXY_CHECK_DONE      = "proxy.check_done"
    PROXY_RESULT          = "proxy.result"
    PROXY_RESULT_BATCH    = "proxy.result_batch"

    # Disk / Storage
    CLEANUP_PROGRESS      = "storage.cleanup_progress"
    CLEANUP_DONE          = "storage.cleanup_done"

    # Registry
    REGISTRY_SCAN_DONE    = "registry.scan_done"

    # Log
    LOG_LINE              = "log.line"

    # App-level
    APP_ERROR             = "app.error"
    APP_NOTIFY            = "app.notify"
    PAGE_CHANGE           = "app.page_change"
    THEME_CHANGE          = "app.theme_change"
    SHUTDOWN              = "app.shutdown"


# Module-level singleton
bus: EventBus = EventBus()
