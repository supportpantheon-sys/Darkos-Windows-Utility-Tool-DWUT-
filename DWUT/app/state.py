"""
AppState — shared runtime state for the entire application.

Modules read from here, never pass the main window around.
UI pages subscribe to EventBus events rather than polling state directly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class _GameBoostState:
    active: bool = False
    game_pid: Optional[int] = None
    game_name: str = ""
    original_priority: Optional[int] = None
    original_power_plan: Optional[str] = None
    # Registry keys saved before modification — restored on deactivate
    registry_backup: dict = field(default_factory=dict)


@dataclass
class _FPSUnlockState:
    active: bool = False
    roblox_pid: Optional[int] = None
    target_fps: int = 240
    patch_count: int = 0
    last_address: Optional[int] = None


@dataclass
class _ProxyState:
    raw_list: list = field(default_factory=list)
    checked_list: list = field(default_factory=list)
    checking_active: bool = False
    fetch_active: bool = False


class AppState:
    """
    Singleton application state.  Access via the module-level `state` object.

    All mutable fields are protected by their own lock where concurrent access
    is expected.  UI pages should NOT store copies of these objects — they
    should read fresh on each access.
    """

    _instance: Optional["AppState"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "AppState":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._init()
                cls._instance = inst
        return cls._instance

    def _init(self) -> None:
        self.is_admin: bool = False          # set during startup
        self.elevation_attempted: bool = False

        self.game_boost = _GameBoostState()
        self.fps_unlock = _FPSUnlockState()
        self.proxy = _ProxyState()

        # Current page shown in the UI (sidebar tracks this)
        self.current_page: str = "dashboard"

        # Whether the sidebar is in icon-only mode
        self.sidebar_collapsed: bool = False

        # Set to True when a shutdown is requested — background loops check this
        self.shutting_down: bool = False

        # Lock for game/fps state (modified from background threads)
        self._boost_lock = threading.Lock()
        self._fps_lock = threading.Lock()

    # ── convenience helpers ───────────────────────────────────────────────────

    def set_game_boost(self, **kwargs) -> None:
        with self._boost_lock:
            for k, v in kwargs.items():
                setattr(self.game_boost, k, v)

    def set_fps_unlock(self, **kwargs) -> None:
        with self._fps_lock:
            for k, v in kwargs.items():
                setattr(self.fps_unlock, k, v)


# Module-level singleton
state: AppState = AppState()
