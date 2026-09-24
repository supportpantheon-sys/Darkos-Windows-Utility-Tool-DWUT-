"""
AppConfig — portable path resolution and all runtime settings.

Portable detection: if a `settings/` folder exists next to the EXE (or next
to this file when running from source), all data is stored there.
Otherwise falls back to %APPDATA%/DWUT/.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal


# ──────────────────────────────────────────────────────────────────────────────
# Portable root detection
# ──────────────────────────────────────────────────────────────────────────────

def _detect_root() -> Path:
    """
    Return the base directory for all DWUT data.

    If a `settings/` folder sits next to the running EXE (or this source file
    when running from source), we're in portable mode — use that location.
    Otherwise use %APPDATA%\\DWUT.
    """
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        exe_dir = Path(sys.executable).parent
    else:
        # Running from source — walk up to the repo root
        exe_dir = Path(__file__).resolve().parent.parent

    portable_marker = exe_dir / "settings"
    if portable_marker.exists() and portable_marker.is_dir():
        return exe_dir

    # Non-portable: use AppData
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "DWUT"


_ROOT: Path = _detect_root()


# ──────────────────────────────────────────────────────────────────────────────
# Directory layout
# ──────────────────────────────────────────────────────────────────────────────

SETTINGS_DIR  = _ROOT / "settings"
LOGS_DIR      = _ROOT / "logs"
BACKUPS_DIR   = _ROOT / "backups"
PROFILES_DIR  = _ROOT / "profiles"
CACHE_DIR     = _ROOT / "cache"

_ALL_DIRS = [SETTINGS_DIR, LOGS_DIR, BACKUPS_DIR, PROFILES_DIR, CACHE_DIR]

def ensure_dirs() -> None:
    """Create all required data directories if they don't exist."""
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Settings schema
# ──────────────────────────────────────────────────────────────────────────────

ThemeName = Literal[
    "dark", "midnight", "obsidian", "abyss",
    "violet", "crimson", "ember", "emerald",
    "nord", "dracula", "solarized", "high_contrast",
    "daylight", "paper",
]

@dataclass
class AppConfig:
    # UI
    theme: ThemeName = "dark"
    window_width: int = 960
    window_height: int = 620
    sidebar_collapsed: bool = False
    start_page: str = "dashboard"

    # Dashboard
    dashboard_refresh_ms: int = 2000
    show_gpu_stats: bool = True

    # Gaming
    restore_on_game_close: bool = True
    fps_unlock_target: int = 240

    # Proxy
    proxy_timeout_s: int = 10
    proxy_max_workers: int = 50
    proxy_default_region: str = "any"
    proxy_default_protocol: str = "any"
    proxy_default_anonymity: str = "elite"

    # Safety
    auto_restore_point: bool = True   # create restore point before destructive ops
    confirm_destructive: bool = True

    # Logging
    log_level: str = "INFO"           # DEBUG | INFO | WARNING | ERROR
    log_max_bytes: int = 5_242_880    # 5 MB
    log_backup_count: int = 3

    # Internal
    _config_version: int = 1

    # ── persistence ──────────────────────────────────────────────────────────

    _CONFIG_FILE: Path = field(default=SETTINGS_DIR / "config.json", init=False, repr=False)

    def save(self) -> None:
        ensure_dirs()
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        self._CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Old theme names → their closest replacement in the current theme set
    _THEME_MIGRATIONS = {
        "carbon": "obsidian",
        "slate":  "abyss",
        "rose":   "crimson",
        "amber":  "ember",
        "teal":   "emerald",
        "green":  "emerald",
    }

    # Exact window sizes that were previously auto-set as THE default (not
    # something a user deliberately resized to) — a saved size that matches
    # one of these gets bumped forward to the current default on load. A
    # user's own manual resize will essentially never land on these exact
    # pixel values, so this is safe.
    _LEGACY_DEFAULT_SIZES = {(1020, 690), (860, 580)}

    @classmethod
    def load(cls) -> "AppConfig":
        ensure_dirs()
        cfg = cls()
        path = SETTINGS_DIR / "config.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for key, val in raw.items():
                    if hasattr(cfg, key) and not key.startswith("_"):
                        setattr(cfg, key, val)
                if cfg.theme in cls._THEME_MIGRATIONS:
                    cfg.theme = cls._THEME_MIGRATIONS[cfg.theme]
                if (cfg.window_width, cfg.window_height) in cls._LEGACY_DEFAULT_SIZES:
                    default = cls()
                    cfg.window_width, cfg.window_height = default.window_width, default.window_height
            except Exception:
                pass  # Corrupt config → fall back to defaults
        return cfg

    def reset(self) -> None:
        """Reset to defaults and save."""
        default = AppConfig()
        for key in asdict(default):
            if not key.startswith("_"):
                setattr(self, key, getattr(default, key))
        self.save()


# Module-level singleton — import this everywhere
config: AppConfig = AppConfig.load()
