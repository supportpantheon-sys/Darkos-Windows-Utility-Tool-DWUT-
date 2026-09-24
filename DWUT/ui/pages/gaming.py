"""
ui/pages/gaming.py — Gaming Suite page.

Contains:
  - FPS Unlocker panel (controls modules/gaming/fps_unlock.py)
  - Game Booster panel (controls modules/gaming/booster.py)

All UI updates come through EventBus callbacks — no direct module calls
from the main thread that could block.
"""

from __future__ import annotations

import customtkinter as ctk

from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from ui.theme import TOKENS, get_font, card_style, button_style

_log = get_logger(__name__)


class GamingPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)
        self._build()
        self._subscribe()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Page title
        ctk.CTkLabel(
            self.frame, text="Gaming Suite",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=20, pady=(16, 12))

        # Two-column layout
        cols = ctk.CTkFrame(self.frame, fg_color="transparent")
        cols.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        left  = ctk.CTkFrame(cols, fg_color="transparent")
        right = ctk.CTkFrame(cols, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right.pack(side="left", fill="both", expand=True)

        self._build_fps_panel(left)
        self._build_boost_panel(right)

    # ── FPS Unlocker panel ─────────────────────────────────────────────────

    def _build_fps_panel(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="both", expand=True)

        # Header
        header = ctk.CTkFrame(card, fg_color=TOKENS["bg_input"], corner_radius=6)
        header.pack(fill="x", padx=12, pady=(12, 8))

        ctk.CTkLabel(
            header, text="FPS Unlocker",
            font=get_font(14, "bold"), text_color=TOKENS["text_primary"]
        ).pack(side="left", padx=12, pady=8)

        self._fps_status_badge = ctk.CTkLabel(
            header, text="● OFF",
            font=get_font(11, "bold"), text_color=TOKENS["error"]
        )
        self._fps_status_badge.pack(side="right", padx=12)

        # Roblox status
        self._roblox_label = ctk.CTkLabel(
            card, text="Roblox: not running",
            font=get_font(11), text_color=TOKENS["text_secondary"]
        )
        self._roblox_label.pack(anchor="w", padx=14, pady=(0, 4))

        # FPS target
        ctk.CTkLabel(
            card, text="Target FPS",
            font=get_font(12), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(8, 2))

        slider_row = ctk.CTkFrame(card, fg_color="transparent")
        slider_row.pack(fill="x", padx=14)

        self._fps_slider = ctk.CTkSlider(
            slider_row,
            from_=60, to=1000,
            number_of_steps=94,
            command=self._on_fps_slider,
            fg_color=TOKENS["bg_input"],
            progress_color=TOKENS["accent"],
            button_color=TOKENS["accent"],
        )
        self._fps_slider.set(240)
        self._fps_slider.pack(side="left", fill="x", expand=True, pady=4)

        self._fps_value_label = ctk.CTkLabel(
            slider_row, text="240",
            font=get_font(14, "bold"), text_color=TOKENS["accent"], width=50
        )
        self._fps_value_label.pack(side="left", padx=8)

        # Presets
        presets_row = ctk.CTkFrame(card, fg_color="transparent")
        presets_row.pack(fill="x", padx=14, pady=8)

        ctk.CTkLabel(
            presets_row, text="Presets:",
            font=get_font(11), text_color=TOKENS["text_secondary"]
        ).pack(side="left")

        for fps in (60, 120, 144, 165, 240, 360, 500):
            ctk.CTkButton(
                presets_row, text=str(fps),
                width=46, height=26,
                fg_color=TOKENS["bg_input"],
                hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_primary"],
                font=get_font(11), corner_radius=4,
                command=lambda f=fps: self._set_fps_preset(f),
            ).pack(side="left", padx=2)

        # Enable / Disable buttons
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(4, 8))

        self._fps_enable_btn = ctk.CTkButton(
            btn_row, text="Enable FPS Unlock",
            **button_style("primary"),
            height=36,
            command=self._enable_fps,
        )
        self._fps_enable_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._fps_disable_btn = ctk.CTkButton(
            btn_row, text="Stop",
            **button_style("ghost"),
            height=36,
            state="disabled",
            command=self._disable_fps,
        )
        self._fps_disable_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Patch log
        ctk.CTkLabel(
            card, text="Activity",
            font=get_font(11), text_color=TOKENS["text_secondary"]
        ).pack(anchor="w", padx=14, pady=(4, 2))

        self._fps_log = ctk.CTkTextbox(
            card,
            fg_color=TOKENS["bg_input"],
            text_color=TOKENS["text_secondary"],
            font=get_font(10, mono=True),
            corner_radius=4,
            height=130,
        )
        self._fps_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._fps_log.configure(state="disabled")

        # Warning note
        ctk.CTkLabel(
            card,
            text="  Uses memory modification (pymem).  Requires administrator.",
            font=get_font(10), text_color=TOKENS["warning"], wraplength=320
        ).pack(padx=12, pady=(0, 10))

    # ── Game Booster panel ─────────────────────────────────────────────────

    def _build_boost_panel(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, **card_style())
        card.pack(fill="both", expand=True)

        header = ctk.CTkFrame(card, fg_color=TOKENS["bg_input"], corner_radius=6)
        header.pack(fill="x", padx=12, pady=(12, 8))

        ctk.CTkLabel(
            header, text="Game Booster",
            font=get_font(14, "bold"), text_color=TOKENS["text_primary"]
        ).pack(side="left", padx=12, pady=8)

        self._boost_badge = ctk.CTkLabel(
            header, text="● OFF",
            font=get_font(11, "bold"), text_color=TOKENS["text_secondary"]
        )
        self._boost_badge.pack(side="right", padx=12)

        # Game name input
        ctk.CTkLabel(
            card, text="Game name (optional)",
            font=get_font(11), text_color=TOKENS["text_secondary"]
        ).pack(anchor="w", padx=14, pady=(4, 2))

        self._game_name_entry = ctk.CTkEntry(
            card,
            placeholder_text="e.g. Rust, Fortnite, Valorant",
            fg_color=TOKENS["bg_input"],
            border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"],
            corner_radius=6,
            height=32,
        )
        self._game_name_entry.pack(fill="x", padx=14, pady=(0, 8))

        # What the booster does (transparency — no hidden claims)
        info_frame = ctk.CTkFrame(card, fg_color=TOKENS["bg_input"], corner_radius=6)
        info_frame.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            info_frame, text="What this does:",
            font=get_font(11, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=10, pady=(8, 4))

        actions = [
            "Sets game process to HIGH priority (real win32 call)",
            "Switches to Ultimate Performance power plan",
            "Enables Hardware GPU Scheduling (registry)",
            "Disables Xbox Game Bar & capture (registry)",
            "Terminates known background hogs, measures real RAM freed",
            "Restores all settings on deactivate",
        ]
        for action in actions:
            ctk.CTkLabel(
                info_frame, text=f"   {action}",
                font=get_font(10), text_color=TOKENS["text_secondary"],
                anchor="w",
            ).pack(fill="x", padx=10, pady=1)

        ctk.CTkFrame(info_frame, fg_color="transparent", height=6).pack()

        # Result labels (populated after activation)
        results_frame = ctk.CTkFrame(card, fg_color="transparent")
        results_frame.pack(fill="x", padx=14, pady=(4, 8))

        self._boost_labels: dict[str, ctk.CTkLabel] = {}
        for key in ("RAM Freed", "Power Plan", "Priority", "GPU Scheduling"):
            row = ctk.CTkFrame(results_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text=f"{key}:", font=get_font(11),
                         text_color=TOKENS["text_secondary"], width=110, anchor="w").pack(side="left")
            lbl = ctk.CTkLabel(row, text="—", font=get_font(11),
                               text_color=TOKENS["text_primary"])
            lbl.pack(side="left")
            self._boost_labels[key] = lbl

        # Buttons
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=8)

        self._boost_enable_btn = ctk.CTkButton(
            btn_row, text="Apply Boost",
            **button_style("primary"), height=36,
            command=self._apply_boost,
        )
        self._boost_enable_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._boost_disable_btn = ctk.CTkButton(
            btn_row, text="Restore",
            **button_style("ghost"), height=36,
            state="disabled",
            command=self._restore_boost,
        )
        self._boost_disable_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        ctk.CTkLabel(
            card,
            text="  All changes are restored on deactivate.  Requires administrator.",
            font=get_font(10), text_color=TOKENS["warning"], wraplength=320
        ).pack(padx=12, pady=(0, 12))

    # ── EventBus subscriptions ─────────────────────────────────────────────

    def _subscribe(self) -> None:
        bus.subscribe(Events.FPS_UNLOCK_STATUS, self._on_fps_status)
        bus.subscribe(Events.FPS_PATCH_APPLIED, self._on_fps_patch)
        bus.subscribe(Events.FPS_ROBLOX_GONE,   self._on_roblox_gone)
        bus.subscribe(Events.GAME_BOOST_CHANGED, self._on_boost_changed)

    def _on_fps_status(self, status) -> None:
        if status is None:
            return
        self._fps_status_badge.configure(
            text="● ON" if status.active else "● OFF",
            text_color=TOKENS["success"] if status.active else TOKENS["error"],
        )
        roblox_text = f"Roblox: running (PID {status.roblox_pid})" if status.roblox_running else "Roblox: not running"
        self._roblox_label.configure(text=roblox_text)
        self._fps_enable_btn.configure(state="disabled" if status.active else "normal")
        self._fps_disable_btn.configure(state="normal" if status.active else "disabled")
        self._fps_log_append(f"[Status] {status.message}")

    def _on_fps_patch(self, data: dict) -> None:
        if data:
            self._fps_log_append(
                f"[Patch] 0x{data.get('address',0):X}  {data.get('old',0):.1f} → {data.get('new',0):.1f} fps"
            )

    def _on_roblox_gone(self, _) -> None:
        self._roblox_label.configure(text="Roblox: closed — waiting for restart")
        self._fps_log_append("[Warning] Roblox process died — will re-attach on restart")

    def _on_boost_changed(self, result) -> None:
        if result is None:
            # Deactivated
            self._boost_badge.configure(text="● OFF", text_color=TOKENS["text_secondary"])
            self._boost_enable_btn.configure(state="normal")
            self._boost_disable_btn.configure(state="disabled")
            for lbl in self._boost_labels.values():
                lbl.configure(text="—")
        else:
            self._boost_badge.configure(text="● ACTIVE", text_color=TOKENS["success"])
            self._boost_enable_btn.configure(state="disabled")
            self._boost_disable_btn.configure(state="normal")
            self._boost_labels["RAM Freed"].configure(
                text=f"{result.ram_freed_mb:.1f} MB (measured)",
                text_color=TOKENS["success"],
            )
            self._boost_labels["Power Plan"].configure(
                text=result.power_plan_applied or "Failed",
                text_color=TOKENS["success"] if result.power_plan_applied else TOKENS["error"],
            )
            self._boost_labels["Priority"].configure(
                text="High" if result.priority_applied else "Not applied",
                text_color=TOKENS["success"] if result.priority_applied else TOKENS["warning"],
            )
            self._boost_labels["GPU Scheduling"].configure(
                text="Enabled" if result.gpu_scheduling_enabled else "Not applied",
                text_color=TOKENS["success"] if result.gpu_scheduling_enabled else TOKENS["warning"],
            )

    # ── Control callbacks ──────────────────────────────────────────────────

    def _on_fps_slider(self, value: float) -> None:
        fps = int(value)
        self._fps_value_label.configure(text=str(fps))
        from modules.gaming.fps_unlock import fps_unlocker
        fps_unlocker.set_target_fps(fps)

    def _set_fps_preset(self, fps: int) -> None:
        self._fps_slider.set(fps)
        self._fps_value_label.configure(text=str(fps))
        from modules.gaming.fps_unlock import fps_unlocker
        fps_unlocker.set_target_fps(fps)

    def _enable_fps(self) -> None:
        from modules.gaming.fps_unlock import fps_unlocker
        fps_unlocker.enable()

    def _disable_fps(self) -> None:
        from modules.gaming.fps_unlock import fps_unlocker
        fps_unlocker.disable()

    def _apply_boost(self) -> None:
        game_name = self._game_name_entry.get().strip() or "Unknown Game"
        from modules.gaming.booster import booster
        worker.submit(
            booster.activate, game_name,
            on_error=lambda e: _log.error("Boost activate failed: %s", e),
        )

    def _restore_boost(self) -> None:
        from modules.gaming.booster import booster
        worker.submit(booster.deactivate)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fps_log_append(self, line: str) -> None:
        self._fps_log.configure(state="normal")
        self._fps_log.insert("end", line.rstrip() + "\n")
        lines = self._fps_log.get("1.0", "end").split("\n")
        if len(lines) > 100:
            self._fps_log.delete("1.0", f"{len(lines)-100}.0")
        self._fps_log.see("end")
        self._fps_log.configure(state="disabled")

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
