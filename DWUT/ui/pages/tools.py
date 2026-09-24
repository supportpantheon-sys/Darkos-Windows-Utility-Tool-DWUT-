"""
ui/pages/tools.py — Tools page.

Sub-sections (tab bar):
  Registry Cleaner  — real scan + real deletion with .reg backup
  Startup Manager   — real winreg + Task Scheduler table with enable/disable
  Disk Cleanup      — real per-category size scan + real deletion
  Storage Info      — drive usage overview

All threading rules:
  Heavy ops run in worker.submit()
  All UI updates via EventBus or frame.after(0, fn)
  messagebox dialogs ONLY on main thread, BEFORE submit
"""

from __future__ import annotations

import customtkinter as ctk

from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from modules.system.disk_cleanup import (
    DiskCleanResult,
    DiskScanResult,
    clean_categories,
    get_drive_info,
    scan_disk_categories,
)
from modules.system.registry_cleaner import (
    CleanResult,
    RegistryIssue,
    ScanResult,
    clean_issues,
    scan_registry,
)
from modules.system.startup import (
    StartupEntry,
    disable_startup_entry,
    enable_startup_entry,
    get_startup_entries,
    open_file_location,
)
from ui.widgets.smooth_scroll import SmoothScrollFrame
from ui.theme import TOKENS, button_style, card_style, get_font

_log = get_logger(__name__)


class ToolsPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)

        # State
        self._reg_issues: list[RegistryIssue] = []
        self._reg_issue_vars: list[ctk.BooleanVar] = []
        self._startup_entries: list[StartupEntry] = []
        self._disk_scan: list = []  # CleanCategory list from scan
        self._disk_cat_vars: dict[str, ctk.BooleanVar] = {}

        self._build()
        self._subscribe()

    # ── Top-level layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        title_row = ctk.CTkFrame(self.frame, fg_color="transparent", height=52)
        title_row.pack(fill="x", padx=20, pady=(14, 0))
        title_row.pack_propagate(False)

        ctk.CTkLabel(
            title_row, text="Tools",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"]
        ).pack(side="left", pady=10)

        self._status_label = ctk.CTkLabel(
            title_row, text="",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        )
        self._status_label.pack(side="right", pady=10)

        tabs = ["Registry Cleaner", "Startup Manager", "Disk Cleanup", "Storage"]
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

        self._content = ctk.CTkFrame(self.frame, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(8, 16))

        # Tabs build lazily — only the first-shown tab is constructed now;
        # the rest build on first switch (same fix as OptimizerPage).
        self._tab_builders = {
            "Registry Cleaner": self._build_registry_tab,
            "Startup Manager":  self._build_startup_tab,
            "Disk Cleanup":     self._build_disk_tab,
            "Storage":          self._build_storage_tab,
        }
        self._built_tabs: set[str] = set()

        self._switch_tab("Registry Cleaner")

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

    # ── Registry Cleaner tab ───────────────────────────────────────────────

    def _build_registry_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Registry Cleaner"] = f

        # Stats bar
        stats = ctk.CTkFrame(f, **card_style())
        stats.pack(fill="x", pady=(0, 8))

        stats_row = ctk.CTkFrame(stats, fg_color="transparent")
        stats_row.pack(fill="x", padx=12, pady=(10, 4))

        self._reg_stats: dict[str, ctk.CTkLabel] = {}
        for key, label, color in [
            ("total",  "Issues: 0",  TOKENS["text_primary"]),
            ("high",   "High: 0",    TOKENS["error"]),
            ("medium", "Medium: 0",  TOKENS["warning"]),
            ("low",    "Low: 0",     TOKENS["success"]),
        ]:
            lbl = ctk.CTkLabel(stats_row, text=label, font=get_font(12, "bold"),
                               text_color=color)
            lbl.pack(side="left", padx=(0, 16))
            self._reg_stats[key] = lbl

        # Action buttons — own row below the stats (5 buttons + 4 stat
        # labels never reliably fit on one line once the sidebar eats
        # into the compact window's width, so give them their own strip)
        btn_row = ctk.CTkFrame(stats, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))

        for label, cmd, variant in [
            ("Scan Registry",      self._start_reg_scan,   "primary"),
            ("Select All",         self._reg_select_all,   "ghost"),
            ("Deselect All",       self._reg_deselect_all, "ghost"),
            ("Create Backup",      self._reg_backup,       "ghost"),
            ("Clean Selected",     self._confirm_reg_clean,"danger"),
        ]:
            ctk.CTkButton(
                btn_row, text=label, **button_style(variant),  # type: ignore
                height=30, command=cmd,
            ).pack(side="left", padx=3)

        # Progress
        self._reg_progress = ctk.CTkProgressBar(
            f, fg_color=TOKENS["bg_input"], progress_color=TOKENS["accent"],
            height=3, corner_radius=2,
        )
        self._reg_progress.set(0)
        self._reg_progress.pack(fill="x", pady=(0, 4))

        # Split panel
        split = ctk.CTkFrame(f, fg_color="transparent")
        split.pack(fill="both", expand=True)

        # Left — issue list
        left = ctk.CTkFrame(split, fg_color=TOKENS["bg_card"], corner_radius=6, width=280)
        left.pack(side="left", fill="both", padx=(0, 6))
        left.pack_propagate(False)

        ctk.CTkLabel(
            left, text="Registry Issues",
            font=get_font(11, "bold"), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self._reg_scroll = SmoothScrollFrame(left, fg_color="transparent")
        self._reg_scroll.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        # Right — log
        right = ctk.CTkFrame(split, fg_color=TOKENS["bg_card"], corner_radius=6)
        right.pack(side="left", fill="both", expand=True)

        ctk.CTkLabel(
            right, text="Scan / Clean Log",
            font=get_font(11, "bold"), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self._reg_log = ctk.CTkTextbox(
            right, fg_color=TOKENS["bg_input"],
            text_color=TOKENS["text_secondary"],
            font=get_font(10, mono=True), corner_radius=4,
        )
        self._reg_log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._reg_log.configure(state="disabled")
        self._reg_log_append("Registry Cleaner ready.\nClick 'Scan Registry' to begin.\n")

    def _start_reg_scan(self) -> None:
        # Clear previous results
        for w in self._reg_scroll.winfo_children():
            w.destroy()
        self._reg_issues.clear()
        self._reg_issue_vars.clear()
        self._reg_progress.set(0)
        self._reg_log_append("\n--- Starting registry scan ---\n")
        self._reg_stats["total"].configure(text="Issues: …")

        def _do() -> ScanResult:
            return scan_registry()

        def _on_done(result: ScanResult) -> None:
            self._reg_issues = result.issues
            self._reg_progress.set(1.0)
            self._reg_log_append(
                f"\nScan complete in {result.scan_duration_s:.1f}s\n"
                f"  Found: {len(result.issues)} issues\n"
                f"  High: {result.high_count}  Medium: {result.medium_count}  Low: {result.low_count}\n"
            )
            self._reg_stats["total"].configure(text=f"Issues: {len(result.issues)}")
            self._reg_stats["high"].configure(text=f"High: {result.high_count}")
            self._reg_stats["medium"].configure(text=f"Medium: {result.medium_count}")
            self._reg_stats["low"].configure(text=f"Low: {result.low_count}")
            self._reg_populate_list(result.issues)
            self._status_label.configure(text=f"Scan done — {len(result.issues)} issues found")

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _reg_populate_list(self, issues: list[RegistryIssue]) -> None:
        for w in self._reg_scroll.winfo_children():
            w.destroy()
        self._reg_issue_vars.clear()

        priority_colors = {
            "High":   TOKENS["error"],
            "Medium": TOKENS["warning"],
            "Low":    TOKENS["text_disabled"],
        }

        if not issues:
            ctk.CTkLabel(
                self._reg_scroll, text="No issues found — registry looks clean.",
                font=get_font(11), text_color=TOKENS["success"],
            ).pack(pady=20)
            return

        for i, issue in enumerate(issues):
            row_color = TOKENS["bg_input"] if i % 2 == 0 else TOKENS["bg_card"]
            row = ctk.CTkFrame(self._reg_scroll, fg_color=row_color, corner_radius=4)
            row.pack(fill="x", pady=1)

            var = ctk.BooleanVar(value=False)
            self._reg_issue_vars.append(var)

            ctk.CTkCheckBox(
                row, text="", variable=var,
                fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
                width=20, border_color=TOKENS["border"],
            ).pack(side="left", padx=8, pady=6)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=4)

            ctk.CTkLabel(
                info, text=issue.description, font=get_font(11, "bold"),
                text_color=TOKENS["text_primary"], anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                info, text=issue.detail[:80], font=get_font(9),
                text_color=TOKENS["text_secondary"], anchor="w",
            ).pack(anchor="w")

            ctk.CTkLabel(
                row, text=issue.priority,
                font=get_font(9, "bold"),
                text_color=priority_colors.get(issue.priority, TOKENS["text_secondary"]),
                width=55,
            ).pack(side="right", padx=8)

    def _reg_select_all(self) -> None:
        for v in self._reg_issue_vars:
            v.set(True)

    def _reg_deselect_all(self) -> None:
        for v in self._reg_issue_vars:
            v.set(False)

    def _reg_backup(self) -> None:
        from modules.system.registry_cleaner import export_registry_backup
        self._reg_log_append("\nCreating registry backup…\n")
        def _do() -> str:
            return export_registry_backup() or "Backup failed"
        def _on_done(path: str) -> None:
            self._reg_log_append(f"Backup saved: {path}\n")
            self._status_label.configure(text=f"Backup: {path}")
        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _confirm_reg_clean(self) -> None:
        selected_issues = [
            issue for issue, var in zip(self._reg_issues, self._reg_issue_vars)
            if var.get()
        ]
        if not selected_issues:
            self._status_label.configure(text="Select at least one issue to clean.")
            return

        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Clean Registry Issues",
            f"Delete {len(selected_issues)} registry entries?\n\n"
            "A .reg backup will be created first so you can restore if needed.\n\n"
            "Continue?",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._reg_log_append(f"\nCleaning {len(selected_issues)} issues…\n")
        self._reg_progress.set(0.1)

        def _do() -> CleanResult:
            return clean_issues(selected_issues, create_backup=True)

        def _on_done(result: CleanResult) -> None:
            self._reg_progress.set(1.0)
            self._reg_log_append(
                f"\n Clean complete in {result.duration_s:.1f}s\n"
                f"  Deleted: {result.deleted}\n"
                f"  Failed:  {len(result.failed)}\n"
                f"  Backup:  {result.backup_path or 'none'}\n"
            )
            if result.failed:
                self._reg_log_append("  Failed items:\n" + "\n".join(f"    {e}" for e in result.failed[:5]))
            self._status_label.configure(
                text=f"Cleaned {result.deleted} entries, {len(result.failed)} failed"
            )
            # Re-run scan to refresh list
            self._start_reg_scan()

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _reg_log_append(self, text: str) -> None:
        self._reg_log.configure(state="normal")
        self._reg_log.insert("end", text)
        lines = self._reg_log.get("1.0", "end").split("\n")
        if len(lines) > 300:
            self._reg_log.delete("1.0", f"{len(lines)-300}.0")
        self._reg_log.see("end")
        self._reg_log.configure(state="disabled")

    # ── Startup Manager tab ────────────────────────────────────────────────

    def _build_startup_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Startup Manager"] = f

        # Toolbar
        toolbar = ctk.CTkFrame(f, **card_style())
        toolbar.pack(fill="x", pady=(0, 8))

        tb_row = ctk.CTkFrame(toolbar, fg_color="transparent")
        tb_row.pack(fill="x", padx=12, pady=10)

        self._startup_count_label = ctk.CTkLabel(
            tb_row, text="Startup entries: —",
            font=get_font(12), text_color=TOKENS["text_secondary"],
        )
        self._startup_count_label.pack(side="left")

        for label, cmd, variant in [
            ("Refresh",           self._load_startup,          "ghost"),
            ("Disable Selected",  self._disable_selected_startup, "danger"),
            ("Enable Selected",   self._enable_selected_startup,  "ghost"),
        ]:
            ctk.CTkButton(
                tb_row, text=label, **button_style(variant),  # type: ignore
                height=30, command=cmd,
            ).pack(side="right", padx=4)

        # Table header
        header = ctk.CTkFrame(f, fg_color=TOKENS["bg_input"], corner_radius=4)
        header.pack(fill="x", pady=(0, 2))

        for col, width in [
            ("", 30), ("Application", 220), ("Location", 110),
            ("Command", 240), ("Impact", 70), ("Status", 70),
        ]:
            ctk.CTkLabel(
                header, text=col, font=get_font(10, "bold"),
                text_color=TOKENS["text_secondary"], width=width, anchor="w",
            ).pack(side="left", padx=4, pady=6)

        # Startup list
        self._startup_scroll = SmoothScrollFrame(
            f, fg_color=TOKENS["bg_card"], corner_radius=6
        )
        self._startup_scroll.pack(fill="both", expand=True)
        self._startup_entry_vars: dict[int, ctk.BooleanVar] = {}

    def _load_startup(self) -> None:
        for w in self._startup_scroll.winfo_children():
            w.destroy()
        self._startup_entry_vars.clear()
        self._startup_count_label.configure(text="Loading…")

        def _do() -> list[StartupEntry]:
            return get_startup_entries()

        def _on_done(entries: list[StartupEntry]) -> None:
            self._startup_entries = entries
            self._startup_count_label.configure(
                text=f"Startup entries: {len(entries)}"
            )
            self._populate_startup_table(entries)

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _populate_startup_table(self, entries: list[StartupEntry]) -> None:
        for w in self._startup_scroll.winfo_children():
            w.destroy()
        self._startup_entry_vars.clear()

        if not entries:
            ctk.CTkLabel(
                self._startup_scroll, text="No startup entries found.",
                font=get_font(12), text_color=TOKENS["text_secondary"],
            ).pack(pady=20)
            return

        impact_colors = {
            "High":    TOKENS["error"],
            "Medium":  TOKENS["warning"],
            "Low":     TOKENS["success"],
            "Unknown": TOKENS["text_disabled"],
        }

        for i, entry in enumerate(entries):
            row_color = TOKENS["bg_input"] if i % 2 == 0 else TOKENS["bg_card"]
            row = ctk.CTkFrame(self._startup_scroll, fg_color=row_color, corner_radius=4)
            row.pack(fill="x", pady=1)

            var = ctk.BooleanVar(value=False)
            self._startup_entry_vars[i] = var

            ctk.CTkCheckBox(
                row, text="", variable=var,
                fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
                width=20, border_color=TOKENS["border"],
            ).pack(side="left", padx=8, pady=6)

            name_label = ctk.CTkLabel(
                row, text=entry.name[:22],
                font=get_font(11, "bold"), text_color=TOKENS["text_primary"],
                width=170, anchor="w",
            )
            name_label.pack(side="left", padx=2)

            ctk.CTkLabel(
                row, text=entry.location[:16],
                font=get_font(10), text_color=TOKENS["text_secondary"],
                width=90, anchor="w",
            ).pack(side="left", padx=2)

            ctk.CTkLabel(
                row, text=entry.command[:26] + "…" if len(entry.command) > 26 else entry.command,
                font=get_font(9, mono=True), text_color=TOKENS["text_secondary"],
                width=170, anchor="w",
            ).pack(side="left", padx=2)

            impact = entry.impact_from_path()
            ctk.CTkLabel(
                row, text=impact,
                font=get_font(9, "bold"),
                text_color=impact_colors.get(impact, TOKENS["text_disabled"]),
                width=60,
            ).pack(side="left", padx=2)

            status_color = TOKENS["success"] if entry.enabled else TOKENS["error"]
            status_text  = "Enabled" if entry.enabled else "Disabled"
            ctk.CTkLabel(
                row, text=status_text,
                font=get_font(9, "bold"), text_color=status_color, width=60,
            ).pack(side="left", padx=2)

            # Open location button
            ctk.CTkButton(
                row, text="",
                fg_color="transparent", hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_secondary"],
                width=28, height=26, corner_radius=4,
                command=lambda e=entry: worker.submit(open_file_location, e),
            ).pack(side="right", padx=6)

    def _get_selected_startup(self) -> list[StartupEntry]:
        return [
            self._startup_entries[i]
            for i, var in self._startup_entry_vars.items()
            if var.get() and i < len(self._startup_entries)
        ]

    def _disable_selected_startup(self) -> None:
        selected = self._get_selected_startup()
        if not selected:
            self._status_label.configure(text="Select entries to disable.")
            return
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Disable Startup Entries",
            f"Disable {len(selected)} startup entries?\n\n"
            "This uses the same method as Task Manager — entries are not deleted,\n"
            "just prevented from starting at login.",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        def _do() -> int:
            count = 0
            for entry in selected:
                if disable_startup_entry(entry):
                    count += 1
            return count

        def _on_done(count: int) -> None:
            self._status_label.configure(text=f"Disabled {count}/{len(selected)} entries")
            self._load_startup()

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _enable_selected_startup(self) -> None:
        selected = self._get_selected_startup()
        if not selected:
            self._status_label.configure(text="Select entries to enable.")
            return

        def _do() -> int:
            count = 0
            for entry in selected:
                if enable_startup_entry(entry):
                    count += 1
            return count

        def _on_done(count: int) -> None:
            self._status_label.configure(text=f"Enabled {count}/{len(selected)} entries")
            self._load_startup()

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    # ── Disk Cleanup tab ───────────────────────────────────────────────────

    def _build_disk_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Disk Cleanup"] = f

        # Toolbar
        toolbar = ctk.CTkFrame(f, **card_style())
        toolbar.pack(fill="x", pady=(0, 8))

        tb_row = ctk.CTkFrame(toolbar, fg_color="transparent")
        tb_row.pack(fill="x", padx=12, pady=10)

        self._disk_total_label = ctk.CTkLabel(
            tb_row, text="Total cleanable: not scanned",
            font=get_font(12), text_color=TOKENS["text_secondary"],
        )
        self._disk_total_label.pack(side="left")

        for label, cmd, variant in [
            ("Scan Sizes",     self._scan_disk,     "ghost"),
            ("Select All",     self._disk_select_all,   "ghost"),
            ("Clean Selected", self._confirm_disk_clean,"danger"),
        ]:
            ctk.CTkButton(
                tb_row, text=label, **button_style(variant),  # type: ignore
                height=30, command=cmd,
            ).pack(side="right", padx=4)

        # Progress
        self._disk_progress = ctk.CTkProgressBar(
            f, fg_color=TOKENS["bg_input"], progress_color=TOKENS["accent"],
            height=3, corner_radius=2,
        )
        self._disk_progress.set(0)
        self._disk_progress.pack(fill="x", pady=(0, 6))

        self._disk_progress_label = ctk.CTkLabel(
            f, text="",
            font=get_font(10), text_color=TOKENS["text_secondary"],
        )
        self._disk_progress_label.pack(anchor="w")

        # Category list
        self._disk_cat_scroll = SmoothScrollFrame(
            f, fg_color=TOKENS["bg_card"], corner_radius=6
        )
        self._disk_cat_scroll.pack(fill="both", expand=True)

        # Pre-populate categories without sizes (sizes added after scan)
        from modules.system.disk_cleanup import _make_categories
        cats = _make_categories()
        self._disk_scan = cats
        self._populate_disk_categories(cats)

    def _populate_disk_categories(self, categories) -> None:
        for w in self._disk_cat_scroll.winfo_children():
            w.destroy()
        self._disk_cat_vars.clear()

        for i, cat in enumerate(categories):
            row_color = TOKENS["bg_input"] if i % 2 == 0 else TOKENS["bg_card"]
            row = ctk.CTkFrame(self._disk_cat_scroll, fg_color=row_color, corner_radius=4)
            row.pack(fill="x", pady=2)

            var = ctk.BooleanVar(value=not cat.aggressive)
            self._disk_cat_vars[cat.key] = var

            ctk.CTkCheckBox(
                row, text="", variable=var,
                fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
                width=20, border_color=TOKENS["border"],
            ).pack(side="left", padx=10, pady=10)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=8)

            name_row = ctk.CTkFrame(info, fg_color="transparent")
            name_row.pack(anchor="w")

            ctk.CTkLabel(
                name_row, text=cat.name, font=get_font(12, "bold"),
                text_color=TOKENS["text_primary"],
            ).pack(side="left")

            if cat.aggressive:
                ctk.CTkLabel(
                    name_row, text="Aggressive",
                    font=get_font(9, "bold"), text_color="#000",
                    fg_color=TOKENS["error"], corner_radius=4,
                ).pack(side="left", padx=6)

            ctk.CTkLabel(
                info, text=cat.description, font=get_font(10),
                text_color=TOKENS["text_secondary"],
            ).pack(anchor="w")

            # Size label (updated after scan)
            size_text = f"{cat.estimated_mb:.1f} MB" if cat.estimated_mb is not None else "Not scanned"
            ctk.CTkLabel(
                row, text=size_text,
                font=get_font(11, "bold"),
                text_color=TOKENS["accent"] if cat.estimated_mb else TOKENS["text_disabled"],
                width=90, anchor="e",
            ).pack(side="right", padx=12)

    def _scan_disk(self) -> None:
        self._disk_progress.set(0.1)
        self._disk_progress_label.configure(text="Scanning…")
        self._disk_total_label.configure(text="Scanning…")

        def _do() -> DiskScanResult:
            return scan_disk_categories()

        def _on_done(result: DiskScanResult) -> None:
            self._disk_scan = result.categories
            self._disk_progress.set(1.0)
            mb = result.total_bytes / (1024 * 1024)
            self._disk_total_label.configure(
                text=f"Total cleanable: {mb:.1f} MB"
            )
            self._disk_progress_label.configure(text=f"Scan done — {mb:.1f} MB found")
            self._populate_disk_categories(result.categories)

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _disk_select_all(self) -> None:
        for v in self._disk_cat_vars.values():
            v.set(True)

    def _confirm_disk_clean(self) -> None:
        keys = [k for k, v in self._disk_cat_vars.items() if v.get()]
        if not keys:
            self._status_label.configure(text="Select at least one category.")
            return

        cats = {cat.key: cat for cat in self._disk_scan}
        selected_cats = [cats[k] for k in keys if k in cats]
        aggressive_selected = [c for c in selected_cats if c.aggressive]

        msg = f"Clean {len(keys)} categories?\n\n"
        msg += "\n".join(f"• {c.name} ({c.estimated_mb:.1f} MB)" if c.estimated_mb else f"• {c.name}" for c in selected_cats[:8])
        if aggressive_selected:
            msg += f"\n\n Aggressive categories selected:\n"
            msg += "\n".join(f"  - {c.name}" for c in aggressive_selected)

        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Confirm Disk Cleanup", msg + "\n\nContinue?",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._disk_progress.set(0.1)
        self._disk_progress_label.configure(text="Cleaning…")
        self._status_label.configure(text="Cleaning…")

        def _do() -> DiskCleanResult:
            return clean_categories(keys)

        def _on_done(result: DiskCleanResult) -> None:
            self._disk_progress.set(1.0)
            mb = result.total_mb_freed
            msg2 = f" Cleaned {mb:.1f} MB ({result.files_deleted} items)"
            self._status_label.configure(text=msg2)
            self._disk_progress_label.configure(text=msg2)
            self._disk_total_label.configure(text=f"Last clean: {mb:.1f} MB freed")

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    # ── Storage tab ────────────────────────────────────────────────────────

    def _build_storage_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Storage"] = f

        ctk.CTkLabel(
            f, text="Drive Storage Overview",
            font=get_font(14, "bold"), text_color=TOKENS["text_primary"],
        ).pack(anchor="w", pady=(0, 10))

        self._storage_cards_frame = ctk.CTkFrame(f, fg_color="transparent")
        self._storage_cards_frame.pack(fill="x")

        ctk.CTkButton(
            f, text="Refresh",
            **button_style("ghost"), height=30,
            command=self._load_storage,
        ).pack(anchor="w", pady=8)

    def _load_storage(self) -> None:
        import string
        drives = [
            f"{d}:\\" for d in string.ascii_uppercase
            if __import__("os").path.exists(f"{d}:\\")
        ]

        def _do() -> list[dict]:
            return [get_drive_info(d) for d in drives]

        def _on_done(infos: list[dict]) -> None:
            for w in self._storage_cards_frame.winfo_children():
                w.destroy()
            for info in infos:
                self._add_drive_card(info)

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _add_drive_card(self, info: dict) -> None:
        card = ctk.CTkFrame(self._storage_cards_frame, **card_style())
        card.pack(fill="x", pady=4)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)

        ctk.CTkLabel(
            row, text=info["drive"],
            font=get_font(16, "bold"), text_color=TOKENS["text_primary"],
            width=50,
        ).pack(side="left")

        pct = info["used_pct"]
        color = TOKENS["error"] if pct > 90 else TOKENS["warning"] if pct > 75 else TOKENS["success"]

        bar = ctk.CTkProgressBar(
            row, height=16, fg_color=TOKENS["bg_input"],
            progress_color=color, corner_radius=4,
        )
        bar.set(pct / 100)
        bar.pack(side="left", fill="x", expand=True, padx=12)

        ctk.CTkLabel(
            row,
            text=f"{info['used_gb']:.1f} / {info['total_gb']:.1f} GB  ({pct:.0f}%)",
            font=get_font(11), text_color=TOKENS["text_secondary"],
            width=200, anchor="e",
        ).pack(side="left")

    # ── EventBus ───────────────────────────────────────────────────────────

    def _subscribe(self) -> None:
        bus.subscribe(Events.REPAIR_PROGRESS,  self._on_repair_progress)
        bus.subscribe(Events.CLEANUP_PROGRESS, self._on_cleanup_progress)

    def _on_repair_progress(self, line: str) -> None:
        self._reg_log_append(line + "\n")

    def _on_cleanup_progress(self, line: str) -> None:
        self._disk_progress_label.configure(text=line[:80])

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
