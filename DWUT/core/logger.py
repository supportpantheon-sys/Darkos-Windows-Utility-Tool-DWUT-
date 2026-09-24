"""
Structured logger with rotation and EventBus UI sink.

Replaces the scattered `log_event()` calls in the original.

Usage
─────
    from core.logger import log

    log.info("Registry scan complete — %d issues found", count)
    log.warning("Roblox process died unexpectedly")
    log.error("Failed to create restore point: %s", exc)
    log.debug("Patched address 0x%X → %.1f fps", addr, fps)

UI pages subscribe to Events.LOG_LINE to receive log records and append
them to their textbox widgets.  The record is a LogRecord namedtuple.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ── LogRecord shipped on the EventBus ────────────────────────────────────────

@dataclass(frozen=True)
class LogRecord:
    level: str       # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
    message: str
    timestamp: datetime
    module: str


# ── EventBus sink handler (publishes to UI) ───────────────────────────────────

class _BusSink(logging.Handler):
    """Forwards log records to the EventBus so UI pages can display them."""

    def __init__(self) -> None:
        super().__init__()
        self._recent: deque[LogRecord] = deque(maxlen=1000)   # in-memory ring buffer

    def emit(self, record: logging.LogRecord) -> None:
        from core.events import Events, bus   # late import — logger initialised early

        lr = LogRecord(
            level=record.levelname,
            message=record.getMessage(),
            timestamp=datetime.fromtimestamp(record.created),
            module=record.name,
        )
        self._recent.append(lr)

        try:
            bus.publish(Events.LOG_LINE, lr)
        except Exception:
            pass   # Never let logging break anything

    @property
    def recent(self) -> list[LogRecord]:
        return list(self._recent)


# ── Logger factory ────────────────────────────────────────────────────────────

_bus_sink: _BusSink = _BusSink()
_setup_lock = threading.Lock()
_configured = False


def _setup_logging(log_level: str = "INFO") -> None:
    global _configured
    with _setup_lock:
        if _configured:
            return
        _configured = True

    from app.config import LOGS_DIR, config
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)   # capture everything; handlers filter

    # ── File handler (rotating) ───────────────────────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        filename=LOGS_DIR / "dwut.log",
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    # ── EventBus sink ─────────────────────────────────────────────────────
    _bus_sink.setLevel(level)
    _bus_sink.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(_bus_sink)

    # ── Console (dev / CLI mode) ──────────────────────────────────────────
    if sys.stdout.isatty():
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter("%(levelname)-8s %(name)s  %(message)s"))
        root.addHandler(console)


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.  Call `_setup_logging()` first (done in main.py).
    Modules should call this at import time:

        from core.logger import get_logger
        _log = get_logger(__name__)
    """
    return logging.getLogger(name)


# ── Module-level convenience logger ──────────────────────────────────────────
# Import `log` for quick one-off logging from any module.
log: logging.Logger = get_logger("dwut")

# Expose the bus sink so UI pages can read recent history on page open
recent_logs: _BusSink = _bus_sink
