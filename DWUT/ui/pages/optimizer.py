"""
ui/pages/optimizer.py — System Optimizer & Windows Utility page.

Sub-sections (implemented as a tab bar inside the page):
  Repairs    — SFC, DISM, Network Reset, WU Repair, Store Repair, WinGet
  Cleanup    — Temp files (real MB measurement), Update cache
  Tweaks     — DNS presets, links to debloat, power plan
  Config     — System tool launchers (Device Manager, Services, etc.)
  Updates    — winget upgrade, Windows Update trigger

Threading rules:
  All heavy ops via worker.submit().
  Output streams into a shared textbox via Events.REPAIR_PROGRESS.
  Confirm dialogs on main thread BEFORE submitting to worker.
"""

from __future__ import annotations

import customtkinter as ctk

from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from modules.system.winutil import (
    clean_temp_files,
    clear_update_cache,
    check_for_updates,
    enable_autologon,
    disable_autologon,
    enable_ntp_sync,
    flush_dns,
    get_available_tools,
    get_dns_presets,
    launch_tool,
    repair_microsoft_store,
    repair_winget,
    repair_windows_update,
    reset_network_stack,
    reset_winsock,
    run_dism,
    run_sfc,
    run_sfc_then_dism,
    set_dns,
    update_all_apps,
)
from modules.system.power_modes import (
    enable_ultimate_performance,
    enable_extreme_performance,
    revert_to_balanced,
    is_ultimate_performance_active,
    is_extreme_performance_active,
)
from modules.debloat.operations import (
    FEATURES,
    FEATURES_BY_KEY,
    apply_features,
    create_restore_point,
)
from modules.system.windows_features import (
    WINDOWS_FEATURES,
    enable_features_batch,
    enable_legacy_f8_recovery,
    disable_legacy_f8_recovery,
    enable_registry_backup_task,
)
from typing import Optional
from ui.widgets.smooth_scroll import SmoothScrollFrame
from ui.theme import TOKENS, button_style, card_style, get_font

_log = get_logger(__name__)


class OptimizerPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)
        self._feature_vars: dict[str, "ctk.BooleanVar"] = {}
        self._build()
        self._subscribe()

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Title
        title_row = ctk.CTkFrame(self.frame, fg_color="transparent", height=52)
        title_row.pack(fill="x", padx=20, pady=(14, 0))
        title_row.pack_propagate(False)

        ctk.CTkLabel(
            title_row, text="System Optimizer & Windows Utility",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"]
        ).pack(side="left", pady=10)

        self._status_label = ctk.CTkLabel(
            title_row, text="",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        )
        self._status_label.pack(side="right", pady=10)

        # Tab bar
        tabs = ["Repairs", "Cleanup", "Tweaks", "Config Tools", "Updates"]
        self._tab_frames: dict[str, ctk.CTkFrame] = {}
        self._tab_btns: dict[str, ctk.CTkButton] = {}

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

        # Shared output log at bottom (all repair output streams here)
        output_card = ctk.CTkFrame(self.frame, **card_style())
        output_card.pack(side="bottom", fill="x", padx=20, pady=(0, 8))

        out_header = ctk.CTkFrame(output_card, fg_color="transparent")
        out_header.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            out_header, text="Output",
            font=get_font(11, "bold"), text_color=TOKENS["text_secondary"]
        ).pack(side="left")

        ctk.CTkButton(
            out_header, text="Clear",
            fg_color="transparent", hover_color=TOKENS["bg_hover"],
            text_color=TOKENS["text_disabled"], font=get_font(10),
            height=22, width=44, corner_radius=4,
            command=self._clear_output,
        ).pack(side="right")

        self._output = ctk.CTkTextbox(
            output_card,
            fg_color=TOKENS["bg_input"],
            text_color=TOKENS["text_secondary"],
            font=get_font(10, mono=True),
            corner_radius=4,
            height=140,
        )
        self._output.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._output.configure(state="disabled")

        # Content area above output
        self._content = ctk.CTkFrame(self.frame, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(6, 4))

        # Tabs build lazily — only the first-shown tab is constructed now;
        # the rest build on first switch. Building all 5 up front (Tweaks
        # alone is 70+ switch cards) was the actual cause of the hitch when
        # first opening this page — same class of fix as the page-level
        # lazy loading, just one level deeper (tabs within a page).
        self._tab_builders: dict[str, "Callable[[], None]"] = {
            "Repairs":      self._build_repairs_tab,
            "Cleanup":      self._build_cleanup_tab,
            "Tweaks":       self._build_tweaks_tab,
            "Config Tools": self._build_config_tab,
            "Updates":      self._build_updates_tab,
        }
        self._built_tabs: set[str] = set()

        self._switch_tab("Repairs")

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

    # ── Repairs tab ────────────────────────────────────────────────────────

    def _build_repairs_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Repairs"] = f

        repairs = [
            (
                "SFC /scannow",
                "Scans and repairs protected Windows system files",
                "5–20 min",
                lambda: self._confirm_run("Run SFC /scannow?\nThis scans and repairs Windows system files.\nTakes 5–20 minutes.", run_sfc),
                "ghost",
            ),
            (
                "DISM RestoreHealth",
                "Repairs the Windows component store from Windows Update",
                "10–30 min",
                lambda: self._confirm_run("Run DISM /RestoreHealth?\nTakes 10–30 minutes and requires internet access.", run_dism),
                "ghost",
            ),
            (
                "SFC + DISM (Recommended)",
                "Runs SFC first, then DISM — the most thorough repair sequence",
                "15–50 min",
                lambda: self._confirm_run("Run SFC then DISM?\nThis is the most thorough repair — takes 15–50 minutes.", run_sfc_then_dism),
                "primary",
            ),
            (
                "Network Stack Reset",
                "Resets TCP/IP, Winsock, firewall rules, and flushes DNS",
                "~30 sec",
                lambda: self._confirm_run("Reset network stack?\nThis resets TCP/IP, Winsock, and flushes DNS.\nA restart may be required.", reset_network_stack),
                "ghost",
            ),
            (
                "Winsock Reset",
                "Resets the Windows Sockets catalog — fixes many connectivity issues",
                "~5 sec",
                lambda: worker.submit(reset_winsock),
                "ghost",
            ),
            (
                "Flush DNS Cache",
                "Clears the DNS resolver cache",
                "~1 sec",
                lambda: worker.submit(flush_dns),
                "ghost",
            ),
            (
                "Windows Update Repair",
                "Stops WU services, clears SoftwareDistribution cache, restarts services",
                "~2 min",
                lambda: self._confirm_run("Repair Windows Update?\nThis stops WU services, clears the download cache, and restarts them.", repair_windows_update),
                "ghost",
            ),
            (
                "Microsoft Store Repair",
                "Re-registers and resets the Microsoft Store app",
                "~1 min",
                lambda: worker.submit(repair_microsoft_store),
                "ghost",
            ),
            (
                "WinGet Repair",
                "Re-registers the Windows Package Manager (winget)",
                "~30 sec",
                lambda: worker.submit(repair_winget),
                "ghost",
            ),
            (
                "NTP Time Sync",
                "Enables Windows Time service and force-syncs the clock against time.windows.com",
                "~5 sec",
                lambda: worker.submit(enable_ntp_sync),
                "ghost",
            ),
        ]

        for name, desc, duration, cmd, variant in repairs:
            self._add_action_row(f, name, desc, duration, cmd, variant)  # type: ignore

        # AutoLogon
        al_card = ctk.CTkFrame(f, **card_style())
        al_card.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(
            al_card, text="AutoLogon", font=get_font(13, "bold"),
            text_color=TOKENS["text_primary"],
        ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(
            al_card,
            text="Skips the login screen and signs in as this user automatically at boot. "
                 "The password is stored in the registry, readable by anyone with admin access "
                 "to this PC — only use this on a machine you're the sole user of.",
            font=get_font(10), text_color=TOKENS["text_secondary"],
            wraplength=560, justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 8))

        al_row = ctk.CTkFrame(al_card, fg_color="transparent")
        al_row.pack(fill="x", padx=14, pady=(0, 12))

        self._autologon_user = ctk.CTkEntry(al_row, placeholder_text="Username", width=160)
        self._autologon_user.pack(side="left", padx=(0, 6))
        self._autologon_pass = ctk.CTkEntry(al_row, placeholder_text="Password", show="•", width=160)
        self._autologon_pass.pack(side="left", padx=6)

        ctk.CTkButton(
            al_row, text="Enable", **button_style("primary"), height=30, width=90,
            command=self._enable_autologon,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            al_row, text="Disable", **button_style("ghost"), height=30, width=90,
            command=lambda: worker.submit(disable_autologon),
        ).pack(side="left", padx=4)

    def _enable_autologon(self) -> None:
        username = self._autologon_user.get().strip()
        password = self._autologon_pass.get()
        if not username or not password:
            self._set_status("Enter both a username and password for AutoLogon.")
            return
        self._confirm_run(
            f"Enable AutoLogon for '{username}'?\n\n"
            "Windows will sign in as this user automatically at boot, skipping the login "
            "screen entirely. The password is saved in the registry in a readable form.",
            lambda: enable_autologon(username, password),
        )

    # ── Cleanup tab ────────────────────────────────────────────────────────

    def _build_cleanup_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Cleanup"] = f

        ctk.CTkLabel(
            f, text="All cleanup operations measure actual bytes freed — no fake numbers.",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", pady=(0, 8))

        items = [
            (
                "Clean Temp Files",
                "Deletes files from %TEMP%, Windows\\Temp, INetCache, and Prefetch",
                "~10 sec",
                self._run_temp_clean,
                "primary",
            ),
            (
                "Clear Windows Update Cache",
                "Stops WU, clears SoftwareDistribution\\Download, restarts WU",
                "~30 sec",
                self._run_update_cache_clean,
                "ghost",
            ),
            (
                "Flush DNS Cache",
                "Clears the DNS resolver cache",
                "~1 sec",
                lambda: worker.submit(flush_dns),
                "ghost",
            ),
            (
                "Open Disk Cleanup (System)",
                "Launches the built-in Windows Disk Cleanup tool",
                "—",
                lambda: launch_tool("Disk Cleanup"),
                "ghost",
            ),
        ]

        for name, desc, duration, cmd, variant in items:
            self._add_action_row(f, name, desc, duration, cmd, variant)  # type: ignore

    def _run_temp_clean(self) -> None:
        self._log("Cleaning temp files…")
        def _do():
            return clean_temp_files()
        def _on_done(result) -> None:
            self._log(
                f"Temp cleanup done — {result.files_deleted} items deleted, "
                f"{result.mb_freed:.1f} MB freed"
            )
            self._set_status(f"Cleaned {result.mb_freed:.1f} MB")
        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _run_update_cache_clean(self) -> None:
        self._log("Clearing Windows Update cache…")
        def _do():
            return clear_update_cache()
        def _on_done(result) -> None:
            self._log(f"Update cache cleared — {result.mb_freed:.1f} MB freed")
        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    # ── Tweaks tab ─────────────────────────────────────────────────────────

    def _build_tweaks_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Tweaks"] = f

        # Power Mode section
        power_card = ctk.CTkFrame(f, **card_style())
        power_card.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            power_card, text="Performance Plans — NOT FOR LAPTOPS ON BATTERY",
            font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(
            power_card,
            text="Ultimate Performance unhides Windows' own high-performance plan. Extreme goes "
                 "further (USB/PCIe power-saving off, CPU pinned to 100%) — best for a desktop or "
                 "a laptop that's staying plugged in, since it noticeably increases power draw and "
                 "heat. Only one can be active at a time; turning one off reverts to Balanced.",
            font=get_font(10), text_color=TOKENS["text_secondary"],
            wraplength=560, justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 8))

        modes_row = ctk.CTkFrame(power_card, fg_color="transparent")
        modes_row.pack(fill="x", padx=14, pady=(0, 12))

        # Ultimate Performance switch
        ult_box = ctk.CTkFrame(modes_row, fg_color=TOKENS["bg_input"], corner_radius=6)
        ult_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ult_row = ctk.CTkFrame(ult_box, fg_color="transparent")
        ult_row.pack(fill="x", padx=10, pady=8)
        ult_label = ctk.CTkLabel(
            ult_row, text="Ultimate Performance", font=get_font(11, "bold"),
            text_color=TOKENS["text_primary"],
        )
        ult_label.pack(side="left")
        self._ultimate_switch = ctk.CTkSwitch(
            ult_row, text="", width=40,
            progress_color=TOKENS["accent"],
            command=self._toggle_ultimate_performance,
        )
        self._ultimate_switch.pack(side="right")
        # is_ultimate_performance_active()/is_extreme_performance_active() each
        # run a `powercfg` subprocess call — checking synchronously here blocked
        # the UI thread on every first visit to this tab. Query in the
        # background instead and reflect the result once it's back.

        from ui.widgets.tooltip import attach_tooltip
        _ult_tip = (
            "Unhides and activates Windows' own built-in Ultimate Performance power plan.\n\n"
            "Removes small background power-saving throttles Windows normally applies "
            "(core parking, idle timers). Uses somewhat more power than Balanced.\n\n"
            "Fine for laptops plugged in; drains battery noticeably faster on battery power."
        )
        attach_tooltip(ult_label, _ult_tip)
        attach_tooltip(ult_box, _ult_tip)

        # Extreme Performance switch
        ext_box = ctk.CTkFrame(modes_row, fg_color=TOKENS["bg_input"], corner_radius=6)
        ext_box.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ext_row = ctk.CTkFrame(ext_box, fg_color="transparent")
        ext_row.pack(fill="x", padx=10, pady=8)
        ext_label = ctk.CTkLabel(
            ext_row, text="Extreme Performance", font=get_font(11, "bold"),
            text_color=TOKENS["text_primary"],
        )
        ext_label.pack(side="left")
        self._extreme_switch = ctk.CTkSwitch(
            ext_row, text="", width=40,
            progress_color=TOKENS["error"],
            command=self._toggle_extreme_performance,
        )
        self._extreme_switch.pack(side="right")

        def _check_power_modes() -> tuple[bool, bool]:
            return is_ultimate_performance_active(), is_extreme_performance_active()

        def _on_power_modes_checked(result: tuple[bool, bool]) -> None:
            ult_on, ext_on = result
            if ult_on:
                self._ultimate_switch.select()
            if ext_on:
                self._extreme_switch.select()

        worker.submit(_check_power_modes,
                       on_done=lambda r: self.frame.after(0, _on_power_modes_checked, r))

        _ext_tip = (
            "NOT FOR LAPTOPS ON BATTERY.\n\n"
            "Everything Ultimate Performance does, plus: disables USB selective suspend, "
            "disables PCI Express Link State Power Management (ASPM), and pins the CPU "
            "minimum state to 100% so cores never downclock.\n\n"
            "Meant for a desktop, or a laptop that's plugged in and stationary — it "
            "noticeably increases power draw, heat, and fan noise, and will drain a "
            "laptop battery fast if used unplugged."
        )
        attach_tooltip(ext_label, _ext_tip)
        attach_tooltip(ext_box, _ext_tip)

        # DNS section
        dns_card = ctk.CTkFrame(f, **card_style())
        dns_card.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            dns_card, text="DNS Server",
            font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(
            dns_card,
            text="Change DNS on all active network adapters. Cloudflare (1.1.1.1) is fastest for most users.",
            font=get_font(10), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=14, pady=(0, 6))

        dns_row = ctk.CTkFrame(dns_card, fg_color="transparent")
        dns_row.pack(fill="x", padx=14, pady=(0, 10))

        self._dns_var = ctk.StringVar(value="Cloudflare (1.1.1.1)")
        ctk.CTkComboBox(
            dns_row, values=get_dns_presets(), variable=self._dns_var,
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=260,
        ).pack(side="left")

        ctk.CTkButton(
            dns_row, text="Apply DNS",
            **button_style("primary"), height=32,
            command=self._apply_dns,
        ).pack(side="left", padx=8)

        # Quick links
        links_card = ctk.CTkFrame(f, **card_style())
        links_card.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            links_card, text="Quick Actions",
            font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 6))

        quick_items = [
            ("Open Debloat & Privacy",    lambda: bus.publish(Events.PAGE_CHANGE, "debloat"),   "primary"),
            ("Open Gaming Suite",         lambda: bus.publish(Events.PAGE_CHANGE, "gaming"),    "ghost"),
            ("Flush DNS Cache",           lambda: worker.submit(flush_dns),                      "ghost"),
            ("Reset Network Stack",       lambda: self._confirm_run(
                "Reset TCP/IP, Winsock, and flush DNS?", reset_network_stack), "ghost"),
        ]

        row = ctk.CTkFrame(links_card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 12))
        for label, cmd, variant in quick_items:
            ctk.CTkButton(
                row, text=label, **button_style(variant),  # type: ignore
                height=32, command=cmd,
            ).pack(side="left", padx=4)

        # ── Windows Features (installed via DISM — takes longer than a
        # simple reg tweak, some need a restart)
        wf_card = ctk.CTkFrame(f, **card_style())
        wf_card.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            wf_card, text="Windows Features",
            font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(
            wf_card,
            text="Windows optional features, installed/removed via DISM. Some take a minute or two "
                 "and may need a restart to finish.",
            font=get_font(10), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=14, pady=(0, 6))

        wf_btn_row = ctk.CTkFrame(wf_card, fg_color="transparent")
        wf_btn_row.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkButton(
            wf_btn_row, text="Install Selected Features",
            **button_style("primary"), height=30,
            command=self._install_selected_win_features,
        ).pack(side="left")

        self._winfeat_vars: dict[str, ctk.BooleanVar] = {}
        wf_grid = ctk.CTkFrame(wf_card, fg_color="transparent")
        wf_grid.pack(fill="x", padx=12, pady=(0, 4))
        for c in range(3):
            wf_grid.columnconfigure(c, weight=1, uniform="wfcol")

        from ui.widgets.tooltip import attach_tooltip as _attach_wf_tip

        for i, wf in enumerate(WINDOWS_FEATURES):
            var = ctk.BooleanVar(value=False)
            self._winfeat_vars[wf.key] = var
            wf_card_i = ctk.CTkFrame(
                wf_grid, fg_color=TOKENS["bg_input"] if i % 2 == 0 else TOKENS["bg_card"],
                corner_radius=6,
            )
            wf_card_i.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)

            ctk.CTkSwitch(
                wf_card_i, text="", variable=var, width=34,
                progress_color=TOKENS["accent"],
            ).pack(anchor="w", padx=8, pady=(8, 2))
            ctk.CTkLabel(wf_card_i, text=wf.name, font=get_font(11, "bold"),
                         text_color=TOKENS["text_primary"], anchor="w", justify="left",
                         wraplength=210).pack(anchor="w", padx=8)
            short = wf.desc if len(wf.desc) <= 78 else wf.desc[:75] + "…"
            wf_desc_lbl = ctk.CTkLabel(wf_card_i, text=short, font=get_font(9),
                         text_color=TOKENS["text_secondary"], anchor="w", justify="left",
                         wraplength=210)
            wf_desc_lbl.pack(anchor="w", padx=8, pady=(0, 8))
            _attach_wf_tip(wf_card_i, wf.desc)
            _attach_wf_tip(wf_desc_lbl, wf.desc)

        # Legacy F8 Boot Recovery + Registry Backup — not DISM features,
        # handled separately (bcdedit / scheduled task)
        f8_row = ctk.CTkFrame(wf_card, fg_color=TOKENS["bg_input"], corner_radius=4)
        f8_row.pack(fill="x", padx=14, pady=2)
        ctk.CTkLabel(f8_row, text="Legacy F8 Boot Recovery Menu", font=get_font(11, "bold"),
                     text_color=TOKENS["text_primary"]).pack(side="left", padx=10, pady=8)
        self._f8_switch = ctk.CTkSwitch(
            f8_row, text="", width=38, progress_color=TOKENS["accent"],
            command=self._toggle_f8_recovery,
        )
        self._f8_switch.pack(side="right", padx=10)

        regbak_row = ctk.CTkFrame(wf_card, fg_color=TOKENS["bg_card"], corner_radius=4)
        regbak_row.pack(fill="x", padx=14, pady=(2, 12))
        ctk.CTkLabel(regbak_row, text="Registry Backup (Daily Task, 12:30am)", font=get_font(11, "bold"),
                     text_color=TOKENS["text_primary"]).pack(side="left", padx=10, pady=8)
        ctk.CTkButton(
            regbak_row, text="Enable", **button_style("ghost"), height=26, width=80,
            command=lambda: worker.submit(enable_registry_backup_task),
        ).pack(side="right", padx=10)

        # ── Fixes
        fixes_card = ctk.CTkFrame(f, **card_style())
        fixes_card.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            fixes_card, text="Fixes",
            font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
        ).pack(anchor="w", padx=14, pady=(10, 6))

        fixes = [
            ("AutoLogon — Configure",     lambda: self._switch_tab("Repairs"), "ghost"),
            ("Network — Reset",           lambda: self._confirm_run(
                "Reset TCP/IP, Winsock, and flush DNS?", reset_network_stack), "ghost"),
            ("NTP Server — Enable",       lambda: worker.submit(enable_ntp_sync), "ghost"),
            ("System Corruption Scan — Run", lambda: worker.submit(run_sfc_then_dism), "ghost"),
            ("Windows Update — Reset",    lambda: self._confirm_run(
                "Reset Windows Update components? This can take a few minutes.",
                repair_windows_update), "ghost"),
            ("WinGet — Reinstall",        lambda: worker.submit(repair_winget), "ghost"),
        ]
        fixes_grid = ctk.CTkFrame(fixes_card, fg_color="transparent")
        fixes_grid.pack(fill="x", padx=14, pady=(0, 12))
        for i, (label, cmd, variant) in enumerate(fixes):
            ctk.CTkButton(
                fixes_grid, text=label, **button_style(variant),  # type: ignore
                height=32, command=cmd,
            ).grid(row=i // 3, column=i % 3, sticky="ew", padx=4, pady=4)
        for c in range(3):
            fixes_grid.columnconfigure(c, weight=1)

        # ── System Tweaks — every Essential / Advanced / Customize
        # Preferences switch lives here, in this tab, since this is where
        # people look for them.
        tweaks_bar = ctk.CTkFrame(f, **card_style())
        tweaks_bar.pack(fill="x", pady=(0, 8))

        bar_row1 = ctk.CTkFrame(tweaks_bar, fg_color="transparent")
        bar_row1.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(bar_row1, text="System Tweaks:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        ctk.CTkButton(
            bar_row1, text="Recommended",
            **button_style("primary"), height=30,
            command=self._select_recommended_features,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            bar_row1, text="Select All",
            **button_style("ghost"), height=30,
            command=lambda: [v.set(True) for v in self._feature_vars.values()],
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            bar_row1, text="Deselect All",
            **button_style("ghost"), height=30,
            command=lambda: [v.set(False) for v in self._feature_vars.values()],
        ).pack(side="left", padx=4)

        # Apply Selected / Create Restore Point get their OWN row — packing
        # 5 buttons into one row was overflowing the card at this window's
        # width, which squashed "Apply Selected" down to a sliver instead
        # of ever actually wrapping or reflowing (pack() doesn't wrap).
        bar_row2 = ctk.CTkFrame(tweaks_bar, fg_color="transparent")
        bar_row2.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(
            bar_row2, text="Apply Selected",
            **button_style("danger"), height=30,
            command=self._confirm_apply_features,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            bar_row2, text="Create Restore Point",
            **button_style("ghost"), height=30,
            command=lambda: worker.submit(create_restore_point),
        ).pack(side="left", padx=4)

        severity_colors = {
            "Critical": TOKENS["error"],
            "High":     TOKENS["warning"],
            "Medium":   "#FF9500",
            "Low":      TOKENS["text_disabled"],
        }

        from ui.widgets.tooltip import attach_tooltip

        def _build_tweak_card(grid_wrap, feat, i: int, columns: int) -> None:
            card = ctk.CTkFrame(
                grid_wrap,
                fg_color=TOKENS["bg_input"] if i % 2 == 0 else TOKENS["bg_card"],
                corner_radius=4,   # was 6 — rounded-corner compositing is the priciest part of redrawing ~90 of these; a smaller radius is real, measurable savings
                border_width=1 if feat.recommended else 0,
                border_color=TOKENS["success"] if feat.recommended else TOKENS["border"],
            )
            card.grid(row=i // columns, column=i % columns, sticky="nsew", padx=3, pady=3)

            var = ctk.BooleanVar(value=feat.recommended)
            self._feature_vars[feat.key] = var

            top_row = ctk.CTkFrame(card, fg_color="transparent")
            top_row.pack(fill="x", padx=8, pady=(8, 2))

            ctk.CTkSwitch(
                top_row, text="", variable=var,
                onvalue=True, offvalue=False,
                progress_color=TOKENS["accent"],
                button_color=TOKENS["text_primary"],
                width=34,
            ).pack(side="left")

            ctk.CTkLabel(
                top_row, text=feat.severity, font=get_font(8, "bold"),
                text_color=severity_colors.get(feat.severity, TOKENS["text_secondary"]),
            ).pack(side="right")

            if feat.recommended:
                ctk.CTkLabel(
                    top_row, text="★", font=get_font(11, "bold"),
                    text_color=TOKENS["success"],
                ).pack(side="right", padx=4)

            ctk.CTkLabel(
                card, text=feat.name, font=get_font(11, "bold"),
                text_color=TOKENS["text_primary"], anchor="w", justify="left",
                wraplength=210,
            ).pack(anchor="w", padx=8)

            short_desc = feat.desc if len(feat.desc) <= 78 else feat.desc[:75] + "…"
            desc_label = ctk.CTkLabel(
                card, text=short_desc, font=get_font(9),
                text_color=TOKENS["text_secondary"], anchor="w", justify="left",
                wraplength=210,
            )
            desc_label.pack(anchor="w", padx=8, pady=(0, 8))

            # Full description always available on hover, even though
            # the card already shows a (possibly truncated) summary
            attach_tooltip(card, feat.desc)
            attach_tooltip(desc_label, feat.desc)

        def _build_tweaks_grid(sections: list, columns: int = 3) -> None:
            """
            Builds the whole Essential/Advanced/Customize Preferences grid
            (~90 cards total) a batch at a time via after(), instead of
            constructing every card synchronously in one shot. Building
            ~90 real composite widgets (switch + 2 labels each, plus a
            tooltip binding) back to back was what actually made the
            Tweaks tab feel frozen for a couple of seconds on first open —
            this yields control back to the Tk event loop between batches
            so the window stays responsive and the cards visibly populate
            in instead of the UI just hanging until they're all done.
            """
            jobs: list[tuple] = []
            for title, subtitle, items in sections:
                jobs.append(("header", title, subtitle))
                for i, feat in enumerate(items):
                    jobs.append(("card", feat, i))

            state = {"grid_wrap": None, "idx": 0}
            BATCH_SIZE = 9   # ~3 rows worth per tick

            def _process_batch() -> None:
                n = 0
                while state["idx"] < len(jobs) and n < BATCH_SIZE:
                    job = jobs[state["idx"]]
                    state["idx"] += 1
                    if job[0] == "header":
                        _, title, subtitle = job
                        ctk.CTkLabel(
                            f, text=title, font=get_font(13, "bold"),
                            text_color=TOKENS["accent"],
                        ).pack(anchor="w", padx=4, pady=(10, 0))
                        if subtitle:
                            ctk.CTkLabel(
                                f, text=subtitle, font=get_font(10),
                                text_color=TOKENS["text_secondary"],
                            ).pack(anchor="w", padx=4, pady=(0, 6))
                        grid_wrap = ctk.CTkFrame(f, fg_color="transparent")
                        grid_wrap.pack(fill="x", padx=2, pady=(0, 4))
                        for c in range(columns):
                            grid_wrap.columnconfigure(c, weight=1, uniform="tweakcol")
                        state["grid_wrap"] = grid_wrap
                        continue   # headers are cheap — don't count against the batch
                    else:
                        _, feat, i = job
                        _build_tweak_card(state["grid_wrap"], feat, i, columns)
                        n += 1

                if state["idx"] < len(jobs):
                    self.frame.after(1, _process_batch)

            _process_batch()

        essential  = [ft for ft in FEATURES if ft.category == "essential"]
        advanced   = [ft for ft in FEATURES if ft.category == "advanced"]
        preference = [ft for ft in FEATURES if ft.category == "preference"]

        _build_tweaks_grid([
            ("Essential Tweaks", "", essential),
            (
                "Advanced Tweaks — CAUTION",
                "These go further than the essentials above — read each description before enabling.",
                advanced,
            ),
            (
                "Customize Preferences",
                "Cosmetic/UX toggles — taskbar, logon screen, Start Menu, accessibility.",
                preference,
            ),
        ])

    def _install_selected_win_features(self) -> None:
        keys = [k for k, v in self._winfeat_vars.items() if v.get()]
        if not keys:
            self._set_status("Select at least one Windows feature to install.")
            return
        names = [wf.name for wf in WINDOWS_FEATURES if wf.key in keys]
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Install Windows Features",
            f"Install {len(keys)} feature(s)?\n\n" + "\n".join(f"• {n}" for n in names)
            + "\n\nThis can take a few minutes. Some features need a restart to finish.",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return
        self._set_status(f"Installing {len(keys)} Windows feature(s)…")
        worker.submit(
            enable_features_batch, keys,
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Feature install failed: {e}"),
        )

    def _toggle_f8_recovery(self) -> None:
        turning_on = self._f8_switch.get() == 1
        fn = enable_legacy_f8_recovery if turning_on else disable_legacy_f8_recovery

        def _on_done(result) -> None:
            self._log(f"{result.tool} — {'success' if result.success else 'failed'}")
            self._set_status(result.tool)
            if not result.success:
                if turning_on:
                    self._f8_switch.deselect()
                else:
                    self._f8_switch.select()

        worker.submit(fn, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _select_recommended_features(self) -> None:
        for feat in FEATURES:
            self._feature_vars[feat.key].set(feat.recommended)

    def _confirm_apply_features(self) -> None:
        keys = [k for k, v in self._feature_vars.items() if v.get()]
        if not keys:
            self._set_status("Select at least one tweak to apply.")
            return

        names = [FEATURES_BY_KEY[k].name for k in keys if k in FEATURES_BY_KEY]
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Apply Tweaks",
            f"Apply {len(keys)} changes?\n\n"
            + "\n".join(f"• {n}" for n in names[:8])
            + ("\n…and more" if len(names) > 8 else "")
            + "\n\nA restore point will be created first.\n"
            "Some changes require a restart to take effect.",
            parent=self.frame.winfo_toplevel(),
        )
        if not answer:
            return

        self._set_status(f"Applying {len(keys)} tweaks…")
        worker.submit(
            apply_features, keys,
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Tweak apply failed: {e}"),
        )

    def _toggle_ultimate_performance(self) -> None:
        turning_on = self._ultimate_switch.get() == 1
        if turning_on:
            # Only one power mode active at a time
            self._extreme_switch.deselect()
            self._log("Activating Ultimate Performance power plan…")
            def _do():
                return enable_ultimate_performance()
        else:
            self._log("Reverting to Balanced power plan…")
            def _do():
                return revert_to_balanced()

        def _on_done(result) -> None:
            ok, msg = result
            self._log(msg)
            self._set_status(msg)
            if not ok:
                # Reflect actual failure in the switch state
                if turning_on:
                    self._ultimate_switch.deselect()
                else:
                    self._ultimate_switch.select()

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _toggle_extreme_performance(self) -> None:
        turning_on = self._extreme_switch.get() == 1
        if turning_on:
            self._ultimate_switch.deselect()
            self._log("Activating Extreme Performance power plan…")
            def _do():
                return enable_extreme_performance()
        else:
            self._log("Reverting to Balanced power plan…")
            def _do():
                return revert_to_balanced()

        def _on_done(result) -> None:
            ok, msg = result
            self._log(msg)
            self._set_status(msg)
            if not ok:
                if turning_on:
                    self._extreme_switch.deselect()
                else:
                    self._extreme_switch.select()

        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    def _apply_dns(self) -> None:
        preset = self._dns_var.get()
        self._log(f"Setting DNS to {preset}…")
        def _do() -> bool:
            return set_dns(preset)
        def _on_done(ok: bool) -> None:
            msg = f"DNS set to {preset}" if ok else f"DNS change failed — check permissions"
            self._log(msg)
            self._set_status(msg)
        worker.submit(_do, on_done=lambda r: self.frame.after(0, _on_done, r))

    # ── Config Tools tab ───────────────────────────────────────────────────

    def _build_config_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Config Tools"] = f

        from ui.widgets.tooltip import attach_tooltip

        ctk.CTkLabel(
            f, text="Click any button to open the tool directly. Hover for a description.",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", pady=(0, 8))

        tools = get_available_tools()   # now returns list of (name, desc)

        COLS = 4
        current_row = None

        for idx, (tool_name, tool_desc) in enumerate(tools):
            if idx % COLS == 0:
                current_row = ctk.CTkFrame(f, fg_color="transparent")
                current_row.pack(fill="x", pady=3)

            btn = ctk.CTkButton(
                current_row,
                text=tool_name,
                fg_color=TOKENS["bg_card"],
                hover_color=TOKENS["bg_hover"],
                border_color=TOKENS["border"],
                border_width=1,
                text_color=TOKENS["text_primary"],
                font=get_font(11),
                height=38, corner_radius=6,
                command=lambda n=tool_name: launch_tool(n),
            )
            btn.pack(side="left", fill="x", expand=True, padx=3)
            if tool_desc:
                attach_tooltip(btn, tool_desc)

    # ── Updates tab ────────────────────────────────────────────────────────

    def _build_updates_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Updates"] = f

        items = [
            (
                "Check for Windows Updates",
                "Triggers a Windows Update scan via UsoClient",
                "~5 sec",
                lambda: worker.submit(check_for_updates),
                "primary",
            ),
            (
                "Update All Apps (WinGet)",
                "Runs winget upgrade --all to update every installed application",
                "5–30 min",
                lambda: self._confirm_run(
                    "Update all apps with WinGet?\nThis may take 5–30 minutes depending on installed apps.",
                    update_all_apps,
                ),
                "ghost",
            ),
            (
                "Open Windows Update Settings",
                "Opens the Windows Update settings page",
                "—",
                lambda: launch_tool("Windows Update"),
                "ghost",
            ),
            (
                "Repair Windows Update",
                "Stops WU services, clears download cache, restarts them",
                "~2 min",
                lambda: self._confirm_run(
                    "Repair Windows Update service?\nClears the download cache and restarts WU services.",
                    repair_windows_update,
                ),
                "ghost",
            ),
        ]

        for name, desc, duration, cmd, variant in items:
            self._add_action_row(f, name, desc, duration, cmd, variant)  # type: ignore

    # ── Shared helpers ─────────────────────────────────────────────────────

    def _add_action_row(
        self,
        parent: SmoothScrollFrame,
        name: str,
        desc: str,
        duration: str,
        command,
        variant: str = "ghost",
    ) -> None:
        row = ctk.CTkFrame(parent, **card_style())
        row.pack(fill="x", pady=4)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        ctk.CTkLabel(
            info, text=name, font=get_font(12, "bold"),
            text_color=TOKENS["text_primary"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            info, text=desc, font=get_font(10),
            text_color=TOKENS["text_secondary"],
        ).pack(anchor="w")

        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right", padx=12, pady=10)

        if duration != "—":
            ctk.CTkLabel(
                right, text=duration,
                font=get_font(9), text_color=TOKENS["text_disabled"],
            ).pack(anchor="e", pady=(0, 4))

        ctk.CTkButton(
            right, text="Run",
            **button_style(variant),  # type: ignore
            height=30, width=70,
            command=command,
        ).pack(anchor="e")

    def _confirm_run(self, message: str, func) -> None:
        """Show confirm dialog on main thread, then submit to worker."""
        from tkinter import messagebox
        answer = messagebox.askyesno(
            "Confirm", message,
            parent=self.frame.winfo_toplevel(),
        )
        if answer:
            worker.submit(func)

    def _subscribe(self) -> None:
        bus.subscribe(Events.REPAIR_PROGRESS, self._on_repair_line)
        bus.subscribe(Events.REPAIR_DONE,     self._on_repair_done)
        bus.subscribe(Events.CLEANUP_PROGRESS, self._on_repair_line)
        bus.subscribe(Events.CLEANUP_DONE,     self._on_cleanup_done)

    def _on_repair_line(self, line: str) -> None:
        self._log(line)

    def _on_repair_done(self, result) -> None:
        if result is None:
            return
        msg = f" {result.tool} — {'success' if result.success else 'completed with errors'} ({result.duration_s:.0f}s)"
        self._log(msg)
        self._set_status(msg)

    def _on_cleanup_done(self, result) -> None:
        if result is None:
            return
        # This handler receives BOTH winutil.CleanupResult (has .mb_freed)
        # and disk_cleanup.DiskCleanResult (only has total_bytes_freed) —
        # they're published on the same event from two different modules.
        if hasattr(result, "mb_freed"):
            mb = result.mb_freed
        elif hasattr(result, "total_bytes_freed"):
            mb = result.total_bytes_freed / (1024 * 1024)
        else:
            mb = 0.0
        self._log(f" Cleanup done — {result.files_deleted} items, {mb:.1f} MB freed")

    def _log(self, text: str) -> None:
        # Background repair/cleanup tasks can still have a log line in
        # flight when the user navigates away or the theme gets reloaded
        # live (which destroys and rebuilds this whole page's widget tree).
        # Without this guard, that queued callback hits a Tk widget that no
        # longer exists and throws — which is exactly what was spamming the
        # log as "invalid command name ...!ctktextbox.!text".
        try:
            if not self._output.winfo_exists():
                return
        except Exception:
            return
        try:
            self._output.configure(state="normal")
            self._output.insert("end", text.rstrip() + "\n")
            lines = self._output.get("1.0", "end").split("\n")
            if len(lines) > 500:
                self._output.delete("1.0", f"{len(lines)-500}.0")
            self._output.see("end")
            self._output.configure(state="disabled")
        except Exception:
            pass   # widget was torn down mid-call — nothing to log to anymore

    def _clear_output(self) -> None:
        self._output.configure(state="normal")
        self._output.delete("1.0", "end")
        self._output.configure(state="disabled")

    def _set_status(self, msg: str) -> None:
        self._status_label.configure(text=msg)

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
