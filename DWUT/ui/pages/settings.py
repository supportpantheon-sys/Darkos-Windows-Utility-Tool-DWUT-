"""
ui/pages/settings.py — Settings

Sections:
  Appearance   — 12-theme picker with live preview swatches
  Dashboard    — refresh interval, GPU toggle
  Proxy        — default filters, workers, timeout
  Safety       — restore point, confirm, restore-on-close
  About        — version, open logs, open backups
"""

from __future__ import annotations

import subprocess

import customtkinter as ctk

from app.config import LOGS_DIR, BACKUPS_DIR, config
from core.events import Events, bus
from core.logger import get_logger
from ui.widgets.smooth_scroll import SmoothScrollFrame
from ui.theme import (
    TOKENS, all_theme_names, apply_theme, current_theme,
    display_name, button_style, card_style, get_font,
)
from ui.widgets.tooltip import InfoBadge, attach_tooltip

_log = get_logger(__name__)
_VERSION = "1.0.0-dev"


class SettingsPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)
        self._int_vars:    dict[str, ctk.StringVar]  = {}
        self._combo_vars:  dict[str, ctk.StringVar]  = {}
        self._toggle_vars: dict[str, ctk.BooleanVar] = {}
        self._build()

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build(self) -> None:
        title_row = ctk.CTkFrame(self.frame, fg_color="transparent", height=52)
        title_row.pack(fill="x", padx=20, pady=(14, 0))
        title_row.pack_propagate(False)

        ctk.CTkLabel(
            title_row, text="Settings",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"],
        ).pack(side="left", pady=10)

        self._save_status = ctk.CTkLabel(
            title_row, text="",
            font=get_font(11), text_color=TOKENS["success"],
        )
        self._save_status.pack(side="right", pady=10)

        ctk.CTkFrame(self.frame, fg_color=TOKENS["border"], height=1).pack(fill="x", padx=20)

        scroll = SmoothScrollFrame(self.frame, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=20, pady=(10, 0))

        self._build_appearance(scroll)
        self._build_dashboard(scroll)
        self._build_proxy_defaults(scroll)
        self._build_safety(scroll)
        self._build_about(scroll)

        # Sticky save button
        save_bar = ctk.CTkFrame(self.frame, fg_color=TOKENS["bg_card"],
                                corner_radius=0, height=52, border_width=1,
                                border_color=TOKENS["border"])
        save_bar.pack(fill="x", side="bottom")
        save_bar.pack_propagate(False)

        ctk.CTkButton(
            save_bar, text="Save All Settings",
            **button_style("primary"), height=36, width=180,
            command=self._save,
        ).pack(side="right", padx=16, pady=8)

    # ── Appearance ─────────────────────────────────────────────────────────

    def _build_appearance(self, parent) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="x", pady=(0, 12))

        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(hdr, text="Appearance", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(side="left")
        InfoBadge(
            hdr,
            "Choose a colour theme for the entire application.\n\n"
            "Dark/Midnight/Obsidian/Abyss — distinct dark surface palettes\n"
            "Violet/Crimson/Ember/Emerald — dark base tinted toward the accent\n"
            "Nord/Dracula/Solarized       — popular programmer colour schemes\n"
            "High Contrast                — maximum readability\n"
            "Daylight/Paper               — light themes\n\n"
            "Applies instantly — the whole app redraws in the new theme.",
        ).pack(side="left", padx=6)

        # Theme grid — 4 columns of colour swatches
        themes = all_theme_names()
        COLS = 4
        current = current_theme()

        for row_idx in range(0, len(themes), COLS):
            row_frame = ctk.CTkFrame(card, fg_color="transparent")
            row_frame.pack(fill="x", padx=14, pady=3)

            for theme_key in themes[row_idx:row_idx + COLS]:
                self._add_theme_swatch(row_frame, theme_key, current)

        ctk.CTkLabel(
            card,
            text="Theme changes apply instantly. The preview swatch shows accent + background colours.",
            font=get_font(10), text_color=TOKENS["text_disabled"],
        ).pack(anchor="w", padx=14, pady=(4, 12))

    def _add_theme_swatch(self, parent, key: str, current_key: str) -> None:
        from ui.theme import _THEMES, _BASE

        # Resolve accent and bg for the preview swatch
        merged = {**_BASE, **_THEMES.get(key, {})}
        accent = merged.get("accent", _BASE["accent"])
        bg     = merged.get("bg_card", _BASE["bg_card"])

        is_current = key == current_key
        border_col = TOKENS["accent"] if is_current else TOKENS["border"]

        swatch_frame = ctk.CTkFrame(
            parent,
            fg_color=TOKENS["bg_input"],
            corner_radius=8,
            border_width=2,
            border_color=border_col,
            width=110, height=64,
        )
        swatch_frame.pack(side="left", padx=5, pady=2)
        swatch_frame.pack_propagate(False)

        # Colour preview strip at top
        preview = ctk.CTkFrame(swatch_frame, fg_color=bg, corner_radius=6, height=28)
        preview.pack(fill="x", padx=4, pady=(4, 2))
        preview.pack_propagate(False)

        accent_dot = ctk.CTkFrame(preview, fg_color=accent, corner_radius=4, width=24, height=16)
        accent_dot.pack(side="right", padx=4, pady=6)

        # Name + apply button
        name_row = ctk.CTkFrame(swatch_frame, fg_color="transparent")
        name_row.pack(fill="x", padx=4)

        ctk.CTkLabel(
            name_row, text=display_name(key),
            font=get_font(10, "bold" if is_current else "normal"),
            text_color=TOKENS["accent"] if is_current else TOKENS["text_secondary"],
            anchor="w",
        ).pack(side="left")

        if not is_current:
            def _apply(k=key):
                config.theme = k
                config.save()
                apply_theme(k)
                # MainWindow is subscribed to THEME_CHANGE and rebuilds the
                # whole UI tree against the new TOKENS — including recreating
                # this very settings page with the new "Active" swatch — so
                # nothing else needs to happen here.
                bus.publish(Events.THEME_CHANGE, k)

            btn = ctk.CTkButton(
                name_row, text="Use",
                fg_color="transparent",
                hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_secondary"],
                font=get_font(9), height=18, width=28, corner_radius=4,
                command=_apply,
            )
            btn.pack(side="right")
        else:
            ctk.CTkLabel(
                name_row, text="Active",
                font=get_font(9), text_color=TOKENS["accent"],
            ).pack(side="right")

    # ── Dashboard ──────────────────────────────────────────────────────────

    def _build_dashboard(self, parent) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(card, text="Dashboard", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(anchor="w", padx=14, pady=(12, 6))

        self._add_labelled_row(
            card,
            label="Refresh interval (ms)",
            tip="How often the Dashboard updates CPU, RAM, Disk, and GPU stats.\n\n"
                "Lower = more responsive but uses slightly more CPU.\n"
                "Default is 2000ms (2 seconds). Minimum is 500ms.",
            widget=self._int_entry("dashboard_refresh_ms", config.dashboard_refresh_ms, width=100),
        )
        self._add_toggle_row(
            card,
            label="Show GPU stats",
            tip="Display GPU usage and temperature on the Dashboard.\n\n"
                "Requires OpenHardwareMonitor to be running. "
                "If OHM is not running, GPU cards will show 'N/A'.",
            attr="show_gpu_stats", current=config.show_gpu_stats,
        )
        ctk.CTkFrame(card, fg_color="transparent", height=6).pack()

    # ── Proxy defaults ─────────────────────────────────────────────────────

    def _build_proxy_defaults(self, parent) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(card, text="Proxy Defaults", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(anchor="w", padx=14, pady=(12, 6))

        self._add_labelled_row(
            card, "Max concurrent workers",
            "Number of proxies checked simultaneously.\n\n"
            "Higher = faster but uses more CPU and RAM.\n"
            "50 is a good balance. Reduce if you see system slowdown.",
            self._int_entry("proxy_max_workers", config.proxy_max_workers, width=80),
        )
        self._add_labelled_row(
            card, "Check timeout (seconds)",
            "Maximum time to wait for each proxy to respond during a check.\n\n"
            "Lower = faster but may incorrectly fail slow but working proxies.\n"
            "Default: 10 seconds.",
            self._int_entry("proxy_timeout_s", config.proxy_timeout_s, width=80),
        )
        self._add_labelled_row(
            card, "Default anonymity filter",
            "Which anonymity level to pre-select in the Proxy Checker.\n\n"
            "Elite = no identifying headers (most private)\n"
            "Anonymous = some headers stripped\n"
            "Any = accept all (including transparent)",
            self._combo("proxy_default_anonymity", config.proxy_default_anonymity,
                        ["any", "elite", "anonymous"]),
        )
        self._add_labelled_row(
            card, "Default protocol filter",
            "Which protocol to pre-select in the Proxy Generator.\n\n"
            "SOCKS5 is the most capable (UDP support, authentication).\n"
            "'any' fetches all three types.",
            self._combo("proxy_default_protocol", config.proxy_default_protocol,
                        ["any", "http", "socks4", "socks5"]),
        )
        ctk.CTkFrame(card, fg_color="transparent", height=6).pack()

    # ── Safety ─────────────────────────────────────────────────────────────

    def _build_safety(self, parent) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(card, text="Safety", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(anchor="w", padx=14, pady=(12, 6))

        self._add_toggle_row(
            card,
            "Auto-create restore point before destructive operations",
            "Automatically creates a Windows System Restore point before running Debloat, "
            "Feature changes, or Registry Cleaner.\n\n"
            "Strongly recommended. Allows rolling back if something goes wrong.\n"
            "Requires System Protection to be enabled on the C: drive.",
            "auto_restore_point", config.auto_restore_point,
        )
        self._add_toggle_row(
            card,
            "Confirm before destructive operations",
            "Show a confirmation dialog before deleting files, removing packages, "
            "or modifying the registry.\n\n"
            "Disable only if you are certain of what you are doing.",
            "confirm_destructive", config.confirm_destructive,
        )
        self._add_toggle_row(
            card,
            "Restore game boost settings when game closes",
            "When the Game Booster detects the game process has exited, "
            "automatically restore the original power plan, process priority, "
            "and registry settings.\n\n"
            "Keeps your system in its normal state when you are not gaming.",
            "restore_on_game_close", config.restore_on_game_close,
        )
        ctk.CTkFrame(card, fg_color="transparent", height=6).pack()

    # ── About ──────────────────────────────────────────────────────────────

    def _build_about(self, parent) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(card, text="About DWUT", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(anchor="w", padx=14, pady=(12, 6))

        for line in [
            f"Version       {_VERSION}",
            f"Python        {__import__('sys').version.split()[0]}",
            "",
            "All operations use real Windows APIs — no simulated output, no fake numbers.",
            "Every destructive action creates a restore point or backup first.",
            "",
            "Built with CustomTkinter, psutil, pymem, requests, pywin32.",
        ]:
            ctk.CTkLabel(
                card, text=line,
                font=get_font(11 if line else 5),
                text_color=TOKENS["text_secondary"] if line else TOKENS["bg_card"],
                anchor="w",
            ).pack(anchor="w", padx=14, pady=1)

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=12)

        for label, cmd, tip in [
            ("Open Logs Folder",    self._open_logs,    "Open the folder containing dwut.log and rotated log files."),
            ("Open Backups Folder", self._open_backups, "Open the folder containing .reg registry backups and debloat session JSON logs."),
            ("Check for Updates",   self._check_updates,"Check GitHub for a newer release of DWUT."),
        ]:
            b = ctk.CTkButton(btn_row, text=label, **button_style("ghost"), height=30, command=cmd)
            attach_tooltip(b, tip)
            b.pack(side="left", padx=(0, 8))

    # ── Widget factories ───────────────────────────────────────────────────

    def _add_labelled_row(self, card, label: str, tip: str, widget) -> None:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=4)

        hdr = ctk.CTkFrame(row, fg_color="transparent")
        hdr.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(hdr, text=label, font=get_font(11),
                     text_color=TOKENS["text_secondary"], anchor="w").pack(side="left")
        InfoBadge(hdr, tip).pack(side="left", padx=4)

        widget.pack(side="right")

    def _add_toggle_row(self, card, label: str, tip: str, attr: str, current: bool) -> None:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=4)

        hdr = ctk.CTkFrame(row, fg_color="transparent")
        hdr.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(hdr, text=label, font=get_font(11),
                     text_color=TOKENS["text_secondary"], anchor="w").pack(side="left")
        InfoBadge(hdr, tip).pack(side="left", padx=4)

        var = ctk.BooleanVar(value=current)
        self._toggle_vars[attr] = var
        ctk.CTkSwitch(
            row, text="", variable=var,
            fg_color=TOKENS["bg_input"],
            progress_color=TOKENS["accent"],
            button_color=TOKENS["accent"],
            width=44,
        ).pack(side="right")

    def _int_entry(self, attr: str, value: int, width: int = 120) -> ctk.CTkEntry:
        var = ctk.StringVar(value=str(value))
        self._int_vars[attr] = var
        return ctk.CTkEntry(
            self.frame,   # reparented when packed
            textvariable=var,
            fg_color=TOKENS["bg_input"],
            border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"],
            width=width, height=28, corner_radius=4,
        )

    def _combo(self, attr: str, value: str, choices: list[str], width: int = 130) -> ctk.CTkComboBox:
        var = ctk.StringVar(value=value)
        self._combo_vars[attr] = var
        return ctk.CTkComboBox(
            self.frame,
            values=choices, variable=var,
            fg_color=TOKENS["bg_input"],
            border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"],
            width=width,
        )

    # ── Save ───────────────────────────────────────────────────────────────

    def _save(self) -> None:
        for attr, var in self._int_vars.items():
            try:
                setattr(config, attr, int(var.get()))
            except ValueError:
                pass
        for attr, var in self._combo_vars.items():
            setattr(config, attr, var.get())
        for attr, var in self._toggle_vars.items():
            setattr(config, attr, var.get())

        config.save()
        self._save_status.configure(text="Saved")
        _log.info("Settings saved")
        self.frame.after(3000, lambda: self._save_status.configure(text=""))

    # ── Openers ────────────────────────────────────────────────────────────

    def _open_logs(self) -> None:
        try:
            subprocess.Popen(["explorer", str(LOGS_DIR)])
        except Exception as exc:
            _log.warning("Could not open logs: %s", exc)

    def _open_backups(self) -> None:
        try:
            subprocess.Popen(["explorer", str(BACKUPS_DIR)])
        except Exception as exc:
            _log.warning("Could not open backups: %s", exc)

    def _check_updates(self) -> None:
        try:
            subprocess.Popen(["start", "", "https://github.com"], shell=True)
        except Exception:
            pass

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
