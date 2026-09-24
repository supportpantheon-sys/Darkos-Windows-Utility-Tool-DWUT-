"""
ui/pages/debloat.py — Full Debloat & Windows Utility page.

Tabs:
  Bloatware   — per-app checkbox removal with scan, select-all, progress
  Privacy     — privacy & telemetry controls
  Recall      — dedicated Windows Recall killer
  Restore     — create/open system restore points

ALL THREADING RULES:
  - Heavy operations run via worker.submit()
  - All UI updates come through EventBus on the main thread
  - messagebox is NEVER called from a background thread
  - Confirmation dialogs happen BEFORE the worker is submitted
"""

from __future__ import annotations

import customtkinter as ctk

from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from modules.debloat.operations import (
    BLOATWARE_DB,
    PRIVACY_ITEMS,
    PRIVACY_BY_KEY,
    apply_features,
    apply_privacy_items,
    check_recall_status,
    create_restore_point,
    open_system_restore,
    remove_bloatware,
    scan_installed_appx,
)
from ui.widgets.smooth_scroll import SmoothScrollFrame
from ui.theme import TOKENS, button_style, card_style, get_font

_log = get_logger(__name__)


class DebloatPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)

        # Checkbox variable tracking (populated in _build_*)
        self._bloat_vars: dict[str, ctk.BooleanVar] = {}   # pkg pattern → BooleanVar
        self._privacy_vars: dict[str, ctk.BooleanVar] = {} # privacy key → BooleanVar

        # Currently displayed bloatware list (changes with OS dropdown)
        self._current_bloatware: list[dict] = []
        self._installed_pkgs: dict[str, bool] = {}  # pkg → installed

        self._build()
        self._subscribe()

    # ── Top-level layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        # Page title + status
        title_row = ctk.CTkFrame(self.frame, fg_color="transparent", height=52)
        title_row.pack(fill="x", padx=20, pady=(14, 0))
        title_row.pack_propagate(False)

        ctk.CTkLabel(
            title_row, text="Debloat & Windows Utility",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"]
        ).pack(side="left", pady=10)

        self._status_label = ctk.CTkLabel(
            title_row, text="",
            font=get_font(11), text_color=TOKENS["text_secondary"]
        )
        self._status_label.pack(side="right", pady=10)

        # Tab bar
        tabs = ["Bloatware", "Privacy", "Recall", "Restore"]
        self._tab_frames: dict[str, ctk.CTkFrame] = {}
        self._tab_btns:   dict[str, ctk.CTkButton] = {}

        tab_bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        tab_bar.pack(fill="x", padx=20)

        for tab in tabs:
            btn = ctk.CTkButton(
                tab_bar, text=tab,
                fg_color="transparent", hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_secondary"],
                font=get_font(12), height=30, corner_radius=4,
                command=lambda t=tab: self._switch_tab(t),
            )
            btn.pack(side="left", padx=2)
            self._tab_btns[tab] = btn

        ctk.CTkFrame(self.frame, fg_color=TOKENS["border"], height=1).pack(fill="x", padx=20)

        # Shared progress bar (visible across all tabs during operations)
        self._progress_bar = ctk.CTkProgressBar(
            self.frame, fg_color=TOKENS["bg_input"],
            progress_color=TOKENS["accent"], height=3, corner_radius=0,
        )
        self._progress_bar.set(0)
        self._progress_bar.pack(fill="x", padx=0, pady=0)

        self._progress_label = ctk.CTkLabel(
            self.frame, text="",
            font=get_font(10), text_color=TOKENS["text_secondary"]
        )
        self._progress_label.pack(anchor="w", padx=22, pady=(2, 0))

        # Content area
        self._content = ctk.CTkFrame(self.frame, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(6, 16))

        # Tabs build lazily — only the first-shown tab is constructed now;
        # the rest build on first switch (same fix as OptimizerPage).
        self._tab_builders = {
            "Bloatware": self._build_bloatware_tab,
            "Privacy":   self._build_privacy_tab,
            "Recall":    self._build_recall_tab,
            "Restore":   self._build_restore_tab,
        }
        self._built_tabs: set[str] = set()

        self._switch_tab("Bloatware")

    def _switch_tab(self, tab: str) -> None:
        if tab not in self._built_tabs:
            self._tab_builders[tab]()
            self._built_tabs.add(tab)
        for name, frame in self._tab_frames.items():
            frame.pack_forget()
        for name, btn in self._tab_btns.items():
            is_active = name == tab
            btn.configure(
                text_color=TOKENS["accent"] if is_active else TOKENS["text_secondary"],
                fg_color=TOKENS["sidebar_active"] if is_active else "transparent",
            )
        self._tab_frames[tab].pack(fill="both", expand=True)

    # ── Bloatware tab ──────────────────────────────────────────────────────

    def _build_bloatware_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Bloatware"] = f

        # OS selector + action bar
        top = ctk.CTkFrame(f, **card_style())
        top.pack(fill="x", pady=(0, 8))

        row1 = ctk.CTkFrame(top, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(10, 6))

        ctk.CTkLabel(row1, text="OS:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")

        self._os_var = ctk.StringVar(value="Windows 11")
        ctk.CTkComboBox(
            row1, values=list(BLOATWARE_DB.keys()),
            variable=self._os_var, command=self._reload_bloatware_list,
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=130,
        ).pack(side="left", padx=8)

        for label, cmd, variant in [
            ("Scan Installed",   self._scan_installed,    "ghost"),
            ("Select All",       self._select_all_bloat,  "ghost"),
            ("Deselect All",     self._deselect_all_bloat,"ghost"),
            ("Remove Selected",  self._confirm_remove,    "danger"),
        ]:
            ctk.CTkButton(
                row1, text=label, **button_style(variant),  # type: ignore
                height=30, command=cmd,
            ).pack(side="left", padx=4)

        # Bloatware list
        self._bloat_scroll = SmoothScrollFrame(
            f, fg_color=TOKENS["bg_card"], corner_radius=6
        )
        self._bloat_scroll.pack(fill="both", expand=True)

        self._reload_bloatware_list()

    def _reload_bloatware_list(self, *_) -> None:
        """Rebuild the checkbox list for the selected OS."""
        for w in self._bloat_scroll.winfo_children():
            w.destroy()
        self._bloat_vars.clear()

        os_name = self._os_var.get()
        entries = BLOATWARE_DB.get(os_name, [])
        self._current_bloatware = entries

        if not entries:
            ctk.CTkLabel(
                self._bloat_scroll, text="No database for this OS.",
                font=get_font(12), text_color=TOKENS["text_secondary"],
            ).pack(pady=20)
            return

        for entry in entries:
            self._add_bloat_row(entry)

    def _add_bloat_row(self, entry: dict) -> None:
        pkg  = entry["pkg"]
        name = entry["name"]
        desc = entry["desc"]
        priority = entry["priority"]

        installed = self._installed_pkgs.get(pkg.lower(), None)

        priority_colors = {
            "High":   TOKENS["success"],
            "Medium": TOKENS["warning"],
            "Low":    TOKENS["text_disabled"],
        }

        row = ctk.CTkFrame(
            self._bloat_scroll,
            fg_color=TOKENS["bg_input"] if len(self._bloat_vars) % 2 == 0 else TOKENS["bg_card"],
            corner_radius=4,
        )
        row.pack(fill="x", pady=1, padx=2)

        var = ctk.BooleanVar(value=False)
        self._bloat_vars[pkg] = var

        ctk.CTkCheckBox(
            row, text="", variable=var,
            fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
            width=20, border_color=TOKENS["border"],
        ).pack(side="left", padx=10, pady=8)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, pady=6)

        name_row = ctk.CTkFrame(info, fg_color="transparent")
        name_row.pack(anchor="w")

        ctk.CTkLabel(
            name_row, text=name, font=get_font(12, "bold"),
            text_color=TOKENS["text_primary"]
        ).pack(side="left")

        if installed is True:
            ctk.CTkLabel(
                name_row, text="INSTALLED",
                font=get_font(9, "bold"), text_color="#000000",
                fg_color=TOKENS["warning"], corner_radius=4,
            ).pack(side="left", padx=6)
        elif installed is False:
            ctk.CTkLabel(
                name_row, text="not found",
                font=get_font(9), text_color=TOKENS["text_disabled"],
            ).pack(side="left", padx=6)

        ctk.CTkLabel(
            info, text=desc, font=get_font(10),
            text_color=TOKENS["text_secondary"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            row, text=priority,
            font=get_font(9, "bold"),
            text_color=priority_colors.get(priority, TOKENS["text_secondary"]),
            width=55, anchor="center",
        ).pack(side="right", padx=10)

    def _select_all_bloat(self) -> None:
        for v in self._bloat_vars.values():
            v.set(True)

    def _deselect_all_bloat(self) -> None:
        for v in self._bloat_vars.values():
            v.set(False)

    def _scan_installed(self) -> None:
        """Query PowerShell for installed packages and update rows."""
        self._set_status("Scanning installed packages…")
        self._progress_bar.set(0.1)

        def _do_scan() -> dict[str, bool]:
            return scan_installed_appx()

        def _on_done(result: dict[str, bool]) -> None:
            self._installed_pkgs = result
            self._reload_bloatware_list()
            # Auto-check installed ones
            for pkg, var in self._bloat_vars.items():
                if result.get(pkg.lower(), False):
                    var.set(True)
            found = sum(1 for v in result.values() if v)
            self._set_status(f"Scan complete — {found} bloatware packages found installed")
            self._progress_bar.set(1.0)

        worker.submit(_do_scan, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _confirm_remove(self) -> None:
        """Gather selection, confirm with messagebox (main thread), then submit."""
        selected = [
            entry for entry in self._current_bloatware
            if self._bloat_vars.get(entry["pkg"], ctk.BooleanVar()).get()
        ]

        if not selected:
            self._set_status("Select at least one app to remove.")
            return

        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Confirm Removal",
            f"Remove {len(selected)} selected apps?\n\n"
            "A system restore point will be created first.\n"
            "This action can be undone via System Restore.",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._set_status(f"Removing {len(selected)} packages…")

        worker.submit(
            remove_bloatware, selected,
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Removal failed: {e}"),
        )

    # ── Features tab ───────────────────────────────────────────────────────

    # ── Privacy tab ────────────────────────────────────────────────────────

    def _build_privacy_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Privacy"] = f

        # Preset buttons
        top = ctk.CTkFrame(f, **card_style())
        top.pack(fill="x", pady=(0, 8))

        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(row, text="Quick presets:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")

        ctk.CTkButton(
            row, text="Maximum Privacy",
            **button_style("danger"), height=30,
            command=self._preset_maximum_privacy,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            row, text="Balanced",
            **button_style("ghost"), height=30,
            command=self._preset_balanced_privacy,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            row, text="Apply Selected",
            **button_style("primary"), height=30,
            command=self._confirm_apply_privacy,
        ).pack(side="right", padx=4)

        # Privacy item list
        scroll = SmoothScrollFrame(f, fg_color=TOKENS["bg_card"], corner_radius=6)
        scroll.pack(fill="both", expand=True)

        for item in PRIVACY_ITEMS:
            var = ctk.BooleanVar(value=False)
            self._privacy_vars[item["key"]] = var

            row_f = ctk.CTkFrame(
                scroll,
                fg_color=TOKENS["bg_input"] if list(self._privacy_vars.keys()).index(item["key"]) % 2 == 0 else TOKENS["bg_card"],
                corner_radius=4,
            )
            row_f.pack(fill="x", pady=2, padx=2)

            ctk.CTkCheckBox(
                row_f, text="", variable=var,
                fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
                width=20, border_color=TOKENS["border"],
            ).pack(side="left", padx=10, pady=10)

            info = ctk.CTkFrame(row_f, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=8)

            ctk.CTkLabel(
                info, text=item["title"], font=get_font(12, "bold"),
                text_color=TOKENS["text_primary"],
            ).pack(anchor="w")

            ctk.CTkLabel(
                info, text=item["desc"], font=get_font(10),
                text_color=TOKENS["text_secondary"],
            ).pack(anchor="w")

    def _preset_maximum_privacy(self) -> None:
        for v in self._privacy_vars.values():
            v.set(True)
        self._set_status("Maximum privacy preset selected — click Apply Selected to execute")

    def _preset_balanced_privacy(self) -> None:
        # Balanced: telemetry + ad ID + activity history only
        balanced = {"telemetry_svc", "ad_id", "activity_feed"}
        for key, v in self._privacy_vars.items():
            v.set(key in balanced)
        self._set_status("Balanced privacy preset selected — click Apply Selected to execute")

    def _confirm_apply_privacy(self) -> None:
        keys = [k for k, v in self._privacy_vars.items() if v.get()]
        if not keys:
            self._set_status("Select at least one privacy item.")
            return

        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Apply Privacy Changes",
            f"Apply {len(keys)} privacy changes?\n\n"
            "These will modify registry settings and may\n"
            "require a restart to take full effect.",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._set_status(f"Applying {len(keys)} privacy changes…")
        worker.submit(
            apply_privacy_items, keys,
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Privacy apply failed: {e}"),
        )

    # ── Recall tab ─────────────────────────────────────────────────────────

    def _build_recall_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Recall"] = f

        # Warning header
        header = ctk.CTkFrame(f, fg_color=TOKENS["error"], corner_radius=8)
        header.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            header, text="Windows Recall — AI Screenshot Surveillance",
            font=get_font(14, "bold"), text_color="#FFFFFF"
        ).pack(padx=16, pady=(12, 2))
        ctk.CTkLabel(
            header,
            text="Windows Recall takes screenshots of everything you do and analyses them with AI.\n"
                 "Disable it to protect your privacy.",
            font=get_font(11), text_color="#FFCCCC", wraplength=720,
        ).pack(padx=16, pady=(0, 12))

        # Status
        status_card = ctk.CTkFrame(f, **card_style())
        status_card.pack(fill="x", pady=(0, 10))

        status_row = ctk.CTkFrame(status_card, fg_color="transparent")
        status_row.pack(fill="x", padx=14, pady=12)

        self._recall_status_label = ctk.CTkLabel(
            status_row, text="Status: not checked",
            font=get_font(12, "bold"), text_color=TOKENS["text_secondary"]
        )
        self._recall_status_label.pack(side="left")

        ctk.CTkButton(
            status_row, text="Check Status",
            **button_style("ghost"), height=30,
            command=self._check_recall,
        ).pack(side="right")

        # What will be done
        info_card = ctk.CTkFrame(f, **card_style())
        info_card.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            info_card, text="What disabling Recall does:",
            font=get_font(12, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 4))

        actions = [
            "Disables AI data analysis via HKLM and HKCU registry policy",
            "Disables Windows Copilot / AI sidebar",
            "Disables Activity Feed (EnableActivityFeed = 0)",
            "Stops CDPUserSvc and OneSyncSvc services",
            "Removes Recall app packages via PowerShell",
        ]
        for act in actions:
            ctk.CTkLabel(
                info_card, text=f"   {act}",
                font=get_font(10), text_color=TOKENS["text_secondary"], anchor="w",
            ).pack(fill="x", padx=14, pady=1)

        ctk.CTkFrame(info_card, fg_color="transparent", height=8).pack()

        # Action buttons
        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(fill="x")

        ctk.CTkButton(
            btn_row, text="Disable Windows Recall Now",
            **button_style("danger"), height=42,
            command=self._confirm_disable_recall,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(
            btn_row, text="Check Status",
            **button_style("ghost"), height=42,
            command=self._check_recall,
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        ctk.CTkLabel(
            f,
            text="  Requires administrator privileges.  Restart recommended after disabling.",
            font=get_font(10), text_color=TOKENS["warning"],
        ).pack(pady=10)

    def _check_recall(self) -> None:
        self._recall_status_label.configure(
            text="Checking…", text_color=TOKENS["text_secondary"]
        )

        def _do():
            return check_recall_status()

        def _on_done(result: dict) -> None:
            label = result["label"]
            pct   = result["pct"]
            color_map = {
                "Disabled": TOKENS["success"],
                "Partially disabled": TOKENS["warning"],
                "Active": TOKENS["error"],
            }
            self._recall_status_label.configure(
                text=f"Status: {label} ({result['disabled_count']}/{result['total_checks']} checks passed — {pct}%)",
                text_color=color_map.get(label, TOKENS["text_secondary"]),
            )

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _confirm_disable_recall(self) -> None:
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Disable Windows Recall",
            "This will:\n"
            "• Disable AI screenshot capture via registry policy\n"
            "• Stop Recall-related services\n"
            "• Remove Recall app packages\n\n"
            "Requires administrator rights.\n"
            "A restart is recommended after this operation.\n\n"
            "Continue?",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._set_status("Disabling Windows Recall…")
        # Recall is a feature in our FEATURES list — apply it directly
        worker.submit(
            apply_features, ["recall"],
            on_done=lambda _: self.frame.after(0, self._check_recall),
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Recall disable failed: {e}"),
        )

    # ── Restore tab ────────────────────────────────────────────────────────

    def _build_restore_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Restore"] = f

        card = ctk.CTkFrame(f, **card_style())
        card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            card, text="System Restore",
            font=get_font(14, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(12, 4))

        ctk.CTkLabel(
            card,
            text="Create a restore point before making changes, or open System Restore to roll back.",
            font=get_font(11), text_color=TOKENS["text_secondary"], wraplength=640,
        ).pack(anchor="w", padx=14, pady=(0, 10))

        self._restore_status = ctk.CTkLabel(
            card, text="",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        )
        self._restore_status.pack(anchor="w", padx=14, pady=(0, 8))

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 14))

        ctk.CTkButton(
            btn_row, text="Create Restore Point",
            **button_style("primary"), height=38,
            command=self._create_restore_point,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="Open System Restore",
            **button_style("ghost"), height=38,
            command=lambda: worker.submit(open_system_restore),
        ).pack(side="left")

        # Backup log section
        log_card = ctk.CTkFrame(f, **card_style())
        log_card.pack(fill="both", expand=True)

        ctk.CTkLabel(
            log_card, text="Debloat Session Backups",
            font=get_font(12, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            log_card,
            text="JSON logs of every debloat session are stored in the backups/ folder.",
            font=get_font(10), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=14)

        ctk.CTkButton(
            log_card, text="Open Backups Folder",
            **button_style("ghost"), height=30,
            command=self._open_backups,
        ).pack(anchor="w", padx=14, pady=(8, 12))

    def _create_restore_point(self) -> None:
        self._restore_status.configure(text="Creating restore point…", text_color=TOKENS["text_secondary"])

        def _do() -> bool:
            return create_restore_point("DWUT Manual Restore Point")

        def _on_done(ok: bool) -> None:
            if ok:
                self._restore_status.configure(
                    text=" Restore point created successfully",
                    text_color=TOKENS["success"],
                )
            else:
                self._restore_status.configure(
                    text=" Failed — make sure System Protection is enabled for C:\\",
                    text_color=TOKENS["error"],
                )

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _open_backups(self) -> None:
        from app.config import BACKUPS_DIR
        import subprocess
        try:
            subprocess.Popen(["explorer", str(BACKUPS_DIR)])
        except Exception as exc:
            _log.warning("Could not open backups folder: %s", exc)

    # ── EventBus subscriptions ─────────────────────────────────────────────

    def _subscribe(self) -> None:
        bus.subscribe(Events.DEBLOAT_PROGRESS, self._on_progress)
        bus.subscribe(Events.DEBLOAT_DONE,     self._on_done)

    def _on_progress(self, data: dict) -> None:
        step = data.get("step", "")
        pct  = data.get("pct", 0.0)
        self._progress_bar.set(pct)
        self._progress_label.configure(text=step)
        self._status_label.configure(text=step)

    def _on_done(self, data: dict) -> None:
        applied = data.get("applied", 0)
        failed  = data.get("failed", [])
        msg = f"Done — {applied} applied"
        if failed:
            msg += f", {len(failed)} failed: {', '.join(failed[:3])}"
        self._status_label.configure(text=msg)
        self._progress_bar.set(1.0)
        self._progress_label.configure(text=msg)

        if failed:
            from tkinter import messagebox
            messagebox.showwarning(
                "Partial Completion",
                f"{applied} changes applied successfully.\n\n"
                f"{len(failed)} failed:\n" + "\n".join(f"• {n}" for n in failed[:10]),
                parent=self.frame.winfo_toplevel(),
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "") -> None:
        self._status_label.configure(
            text=msg,
            text_color=color or TOKENS["text_secondary"],
        )
        self._progress_label.configure(text=msg)

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
