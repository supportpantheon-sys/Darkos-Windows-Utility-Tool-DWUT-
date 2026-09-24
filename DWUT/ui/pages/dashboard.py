"""
ui/pages/dashboard.py — fully responsive, no fixed widths, no layout jank.

Layout rules:
  - StatCards use equal-weight grid columns so they always fill available width
  - Detail cards (sys/net/mem) use proportional weights: 2 / 1 / 1
  - All padx/pady use relative values so the page scales smoothly
  - No pack_propagate(False) on resizable containers
  - GPU name label placed correctly AFTER the cards
  - Single persistent background worker — never recreated on page switch
"""

from __future__ import annotations

import threading
from typing import Optional

import customtkinter as ctk

from app.config import config
from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from modules.system.info import (
    DashboardSnapshot,
    SystemHealthScore,
    compute_health_score,
    get_dashboard_snapshot,
    gpu_detection_status,
    gpu_packages_missing,
    install_gpu_packages,
)
from ui.theme import TOKENS, get_font, card_style
from ui.widgets.tooltip import InfoBadge, attach_tooltip

_log = get_logger(__name__)


# ── Stat Card ─────────────────────────────────────────────────────────────────

class StatCard(ctk.CTkFrame):
    """
    Responsive metric card.  Uses grid weight so cards share width equally.
    Does NOT use pack_propagate(False) — it grows with the window.
    """

    def __init__(self, parent, title: str, unit: str = "",
                 color: str = "", tooltip: str = "") -> None:
        super().__init__(
            parent,
            fg_color=TOKENS["bg_card"],
            corner_radius=8,
            border_width=1,
            border_color=TOKENS["border"],
        )
        self._unit   = unit
        self._accent = color or TOKENS["accent"]

        # Title row
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(pady=(10, 2), padx=10)
        ctk.CTkLabel(
            hdr, text=title,
            font=get_font(11), text_color=TOKENS["text_secondary"],
        ).pack(side="left")
        if tooltip:
            InfoBadge(hdr, tooltip).pack(side="left", padx=3)

        # Big value
        self._val_label = ctk.CTkLabel(
            self, text="—",
            font=get_font(24, "bold"), text_color=self._accent,
        )
        self._val_label.pack(padx=10)

        # Progress bar
        self._bar = ctk.CTkProgressBar(
            self, height=3,
            fg_color=TOKENS["bg_input"],
            progress_color=self._accent,
            corner_radius=2,
        )
        self._bar.set(0)
        self._bar.pack(fill="x", padx=12, pady=(4, 10))

    def update_value(self, value: Optional[float], pct: Optional[float] = None) -> None:
        """Update in-place — no widget recreation."""
        if value is None:
            self._val_label.configure(text="N/A", text_color=TOKENS["text_disabled"])
            self._bar.set(0)
        else:
            self._val_label.configure(
                text=f"{value:.0f}{self._unit}",
                text_color=self._accent,
            )
            if pct is not None:
                self._bar.set(max(0.0, min(1.0, pct / 100.0)))

    def set_text(self, text: str, color: Optional[str] = None) -> None:
        """Show an arbitrary string (e.g. 'Detected') without a unit."""
        self._val_label.configure(
            text=text,
            text_color=color or TOKENS["text_secondary"],
        )
        self._bar.set(0)


# ── Detail card helper ────────────────────────────────────────────────────────

def _detail_card(parent, title: str, tip: str = "") -> tuple[ctk.CTkFrame, dict]:
    """Return (card_frame, {key: label}) for a key/value info card."""
    f = ctk.CTkFrame(parent, **card_style())

    hdr = ctk.CTkFrame(f, fg_color="transparent")
    hdr.pack(fill="x", padx=12, pady=(10, 6))
    ctk.CTkLabel(
        hdr, text=title,
        font=get_font(13, "bold"), text_color=TOKENS["text_primary"],
    ).pack(side="left")
    if tip:
        InfoBadge(hdr, tip).pack(side="left", padx=5)

    return f, hdr


# ── Dashboard page ────────────────────────────────────────────────────────────

class DashboardPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)
        self._visible = False
        self._stop_ev: Optional[threading.Event] = None
        self._sys_labels: dict[str, ctk.CTkLabel] = {}
        self._net_labels: dict[str, ctk.CTkLabel] = {}
        self._mem_labels: dict[str, ctk.CTkLabel] = {}

        self._build()
        bus.subscribe(Events.DASHBOARD_STATS, self._on_stats)

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        P = 16   # base padding

        # ── Title row ─────────────────────────────────────────────────────
        title_bar = ctk.CTkFrame(self.frame, fg_color="transparent", height=48)
        title_bar.pack(fill="x", padx=P, pady=(P, 0))
        title_bar.pack_propagate(False)

        ctk.CTkLabel(
            title_bar, text="System Overview",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"],
        ).pack(side="left", pady=10)

        self._health_badge = ctk.CTkLabel(
            title_bar, text="Health: —",
            font=get_font(12, "bold"), text_color=TOKENS["text_secondary"],
        )
        self._health_badge.pack(side="right", pady=10)

        # ── Stat cards — equal-weight grid ────────────────────────────────
        cards_outer = ctk.CTkFrame(self.frame, fg_color="transparent")
        cards_outer.pack(fill="x", padx=P, pady=(10, 8))

        # 5 columns, all equal weight → cards always share available width
        for col in range(5):
            cards_outer.columnconfigure(col, weight=1, uniform="statcard")

        self._cpu_card  = StatCard(cards_outer, "CPU",      "%",  TOKENS["accent"],
            "CPU utilisation across all logical cores.")
        self._ram_card  = StatCard(cards_outer, "RAM",      "%",  TOKENS["success"],
            "Physical memory in use as a percentage of total RAM.")
        self._disk_card = StatCard(cards_outer, "DISK",     "%",  TOKENS["warning"],
            "C: drive usage. High usage slows Windows significantly.")
        self._gpu_card  = StatCard(cards_outer, "GPU",      "%",  TOKENS["info"],
            "GPU core load, any vendor.\n\nUses NVML on NVIDIA, Windows' own "
            "GPU Engine counters otherwise (same source as Task Manager's "
            "GPU graph) — needs the 'wmi' package installed.")
        self._temp_card = StatCard(cards_outer, "GPU °C",   "°",  TOKENS["error"],
            "GPU temperature in Celsius.\nAvailable via pynvml (NVIDIA) or "
            "OpenHardwareMonitor.\nN/A is normal if neither is running — "
            "Windows has no vendor-agnostic temperature counter.")

        for col, card in enumerate((self._cpu_card, self._ram_card,
                                    self._disk_card, self._gpu_card, self._temp_card)):
            card.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)

        # GPU name label below the cards, with a one-click installer for
        # the missing wmi/pynvml packages when that's why it's not detected
        gpu_row = ctk.CTkFrame(cards_outer, fg_color="transparent")
        gpu_row.grid(row=1, column=0, columnspan=5, sticky="e", padx=4, pady=(0, 2))

        self._gpu_install_btn = ctk.CTkButton(
            gpu_row, text="Install GPU Support", width=140, height=22,
            font=get_font(9), fg_color=TOKENS["accent_dim"], hover_color=TOKENS["accent"],
            command=self._install_gpu_packages,
        )
        # Hidden until we actually know packages are missing (see _on_stats)

        self._gpu_name_label = ctk.CTkLabel(
            gpu_row, text="",
            font=get_font(9), text_color=TOKENS["text_disabled"], anchor="e",
        )
        self._gpu_name_label.pack(side="right")

        # ── Detail cards — proportional grid ──────────────────────────────
        detail_outer = ctk.CTkFrame(self.frame, fg_color="transparent")
        detail_outer.pack(fill="x", padx=P, pady=(0, 8))
        detail_outer.columnconfigure(0, weight=3)   # System card wider
        detail_outer.columnconfigure(1, weight=2)   # Network
        detail_outer.columnconfigure(2, weight=2)   # Memory

        sys_card = self._build_sys_card(detail_outer)
        net_card = self._build_net_card(detail_outer)
        mem_card = self._build_mem_card(detail_outer)

        sys_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        net_card.grid(row=0, column=1, sticky="nsew", padx=(0, 4))
        mem_card.grid(row=0, column=2, sticky="nsew")

        # ── Quick actions ──────────────────────────────────────────────────
        qa_card = ctk.CTkFrame(self.frame, **card_style())
        qa_card.pack(fill="x", padx=P, pady=(0, 8))

        qa_hdr = ctk.CTkFrame(qa_card, fg_color="transparent")
        qa_hdr.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(qa_hdr, text="Quick Actions",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
                     ).pack(side="left")

        qa_btns = ctk.CTkFrame(qa_card, fg_color="transparent")
        qa_btns.pack(fill="x", padx=12, pady=(0, 10))
        for col in range(5):
            qa_btns.columnconfigure(col, weight=1)

        quick = [
            ("Optimize & Repair",  "optimizer", "SFC, DISM, network reset, tool launchers"),
            ("Game Booster",       "gaming",    "Real process priority + power plan boost"),
            ("Debloat Windows",    "debloat",   "Remove bloatware, disable telemetry & Recall"),
            ("Proxy Center",       "proxy",     "Fetch, validate and export proxies"),
            ("Tools",              "tools",     "Registry cleaner, startup manager, disk cleanup"),
        ]
        for col, (label, page, tip) in enumerate(quick):
            b = ctk.CTkButton(
                qa_btns, text=label,
                fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
                text_color=TOKENS["accent_text"], font=get_font(12),
                height=32, corner_radius=6,
                command=lambda p=page: bus.publish(Events.PAGE_CHANGE, p),
            )
            b.grid(row=0, column=col, sticky="ew", padx=3)
            attach_tooltip(b, tip)

        # ── Activity log ───────────────────────────────────────────────────
        log_card = ctk.CTkFrame(self.frame, **card_style())
        log_card.pack(fill="both", expand=True, padx=P, pady=(0, P))

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        log_hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(log_hdr, text="Recent Activity",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
                     ).pack(side="left")
        ctk.CTkButton(
            log_hdr, text="Clear",
            fg_color="transparent", hover_color=TOKENS["bg_hover"],
            text_color=TOKENS["text_disabled"], font=get_font(10),
            height=22, width=44, corner_radius=4,
            command=self._clear_log,
        ).pack(side="right")

        self._log_box = ctk.CTkTextbox(
            log_card,
            fg_color=TOKENS["bg_input"],
            text_color=TOKENS["text_secondary"],
            font=get_font(10, mono=True),
            corner_radius=4,
        )
        self._log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._log_box.configure(state="disabled")
        bus.subscribe(Events.LOG_LINE, self._on_log_line)

    def _build_sys_card(self, parent) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, **card_style())
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="System",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]).pack(side="left")

        rows = [
            ("CPU",    "CPU model name"),
            ("OS",     "Windows build number"),
            ("Uptime", "Time since last restart — long uptimes lower the health score"),
            ("Host",   "Machine hostname"),
            ("Admin",  "Administrator rights — most DWUT features require this"),
            ("Reboot", "Whether Windows needs a restart to finish installing updates"),
        ]
        for key, tip in rows:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)

            key_frame = ctk.CTkFrame(row, fg_color="transparent")
            key_frame.pack(side="left")
            ctk.CTkLabel(key_frame, text=f"{key}:",
                         font=get_font(11), text_color=TOKENS["text_secondary"],
                         width=54, anchor="w").pack(side="left")
            InfoBadge(key_frame, tip).pack(side="left", padx=2)

            val = ctk.CTkLabel(
                row, text="—",
                font=get_font(11), text_color=TOKENS["text_primary"],
                anchor="w",
            )
            val.pack(side="left", padx=6, fill="x", expand=True)
            self._sys_labels[key] = val

        ctk.CTkFrame(f, fg_color="transparent", height=6).pack()
        return f

    def _build_net_card(self, parent) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, **card_style())
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="Network",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]).pack(side="left")
        InfoBadge(hdr, "Live network send/receive rates and totals since boot.").pack(side="left", padx=5)

        rows = [
            ("Send Rate", "Current upload speed"),
            ("Recv Rate", "Current download speed"),
            ("Total Sent", "Total bytes sent since boot"),
            ("Total Recv", "Total bytes received since boot"),
        ]
        for key, tip in rows:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            key_f = ctk.CTkFrame(row, fg_color="transparent")
            key_f.pack(side="left")
            ctk.CTkLabel(key_f, text=f"{key}:", font=get_font(11),
                         text_color=TOKENS["text_secondary"], width=74, anchor="w").pack(side="left")
            InfoBadge(key_f, tip).pack(side="left", padx=2)
            val = ctk.CTkLabel(row, text="—", font=get_font(11),
                               text_color=TOKENS["text_primary"], anchor="w")
            val.pack(side="left", padx=6, fill="x", expand=True)
            self._net_labels[key] = val

        ctk.CTkFrame(f, fg_color="transparent", height=6).pack()
        return f

    def _build_mem_card(self, parent) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, **card_style())
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="Memory",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]).pack(side="left")
        InfoBadge(hdr, "Physical RAM usage breakdown.").pack(side="left", padx=5)

        rows = [
            ("Used",  "RAM currently in use"),
            ("Free",  "RAM available for new processes"),
            ("Total", "Total physical RAM installed"),
            ("Usage", "Percentage of RAM in use"),
        ]
        for key, tip in rows:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            key_f = ctk.CTkFrame(row, fg_color="transparent")
            key_f.pack(side="left")
            ctk.CTkLabel(key_f, text=f"{key}:", font=get_font(11),
                         text_color=TOKENS["text_secondary"], width=46, anchor="w").pack(side="left")
            InfoBadge(key_f, tip).pack(side="left", padx=2)
            val = ctk.CTkLabel(row, text="—", font=get_font(11),
                               text_color=TOKENS["text_primary"], anchor="w")
            val.pack(side="left", padx=6, fill="x", expand=True)
            self._mem_labels[key] = val

        ctk.CTkFrame(f, fg_color="transparent", height=6).pack()
        return f

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_show(self) -> None:
        self._visible = True
        if self._stop_ev is None:
            interval = config.dashboard_refresh_ms / 1000.0
            self._stop_ev = worker.submit_periodic(self._do_refresh, interval)

    def on_hide(self) -> None:
        self._visible = False

    def _do_refresh(self) -> None:
        if not self._visible:
            return
        snap = get_dashboard_snapshot()
        bus.publish(Events.DASHBOARD_STATS, snap)

    # ── Update callbacks ───────────────────────────────────────────────────

    def _on_stats(self, snap: DashboardSnapshot) -> None:
        if not self._visible:
            return
        health = compute_health_score(snap)
        self._update_cards(snap)
        self._update_sys(snap)
        self._update_net(snap)
        self._update_mem(snap)
        self._update_health(health)

    def _update_cards(self, snap: DashboardSnapshot) -> None:
        self._cpu_card.update_value(snap.cpu_pct, snap.cpu_pct)
        self._ram_card.update_value(snap.ram_pct, snap.ram_pct)
        self._disk_card.update_value(snap.disk_pct, snap.disk_pct)

        if snap.gpu:
            src_map = {"nvml": "NVML", "ohm": "OHM", "perfcounter": "GPU Engine", "wmi_name": "WMI"}
            src_label = src_map.get(snap.gpu.source, "")
            self._gpu_name_label.configure(
                text=f"{snap.gpu.name}  [{src_label}]" if src_label else snap.gpu.name
            )
            if snap.gpu.usage_pct is not None:
                self._gpu_card.update_value(snap.gpu.usage_pct, snap.gpu.usage_pct)
            else:
                self._gpu_card.set_text("Detected", TOKENS["text_secondary"])

            if snap.gpu.temp_c is not None:
                self._temp_card.update_value(snap.gpu.temp_c)
            else:
                self._temp_card.update_value(None)

            if self._gpu_install_btn.winfo_ismapped():
                self._gpu_install_btn.pack_forget()
        else:
            self._gpu_card.update_value(None)
            self._temp_card.update_value(None)
            self._gpu_name_label.configure(text=f"GPU: not detected — {gpu_detection_status()}")

            if gpu_packages_missing():
                if not self._gpu_install_btn.winfo_ismapped():
                    self._gpu_install_btn.pack(side="right", padx=(0, 8))
            elif self._gpu_install_btn.winfo_ismapped():
                self._gpu_install_btn.pack_forget()

    def _install_gpu_packages(self) -> None:
        self._gpu_install_btn.configure(state="disabled", text="Installing…")

        def _on_done(result: tuple[bool, str]) -> None:
            ok, msg = result
            self._gpu_install_btn.configure(state="normal", text="Install GPU Support")
            from tkinter import messagebox
            if ok:
                messagebox.showinfo("GPU Support", msg, parent=self.frame.winfo_toplevel())
            else:
                messagebox.showerror(
                    "Install Failed",
                    f"Couldn't install GPU packages:\n\n{msg}",
                    parent=self.frame.winfo_toplevel(),
                )

        worker.submit(install_gpu_packages,
                       on_done=lambda r: self.frame.after(0, _on_done, r))

    def _update_sys(self, snap: DashboardSnapshot) -> None:
        def _uptime(td) -> str:
            s = int(td.total_seconds())
            d, r = divmod(s, 86400)
            h, r2 = divmod(r, 3600)
            m = r2 // 60
            return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"

        self._sys_labels["CPU"].configure(text=snap.cpu_name)
        self._sys_labels["OS"].configure(text=snap.windows_version)
        self._sys_labels["Uptime"].configure(text=_uptime(snap.uptime))
        self._sys_labels["Host"].configure(text=snap.hostname)
        self._sys_labels["Admin"].configure(
            text="Yes" if snap.is_admin else "No — restart as admin",
            text_color=TOKENS["success"] if snap.is_admin else TOKENS["warning"],
        )
        self._sys_labels["Reboot"].configure(
            text="Restart pending" if snap.pending_reboot else "None",
            text_color=TOKENS["warning"] if snap.pending_reboot else TOKENS["success"],
        )

    def _update_net(self, snap: DashboardSnapshot) -> None:
        def _rate(v: Optional[float]) -> str:
            if v is None: return "—"
            if v > 1024:  return f"{v/1024:.1f} MB/s"
            return f"{v:.0f} KB/s"

        def _total(b: int) -> str:
            for u in ("B", "KB", "MB", "GB", "TB"):
                if b < 1024: return f"{b:.1f} {u}"
                b //= 1024
            return f"{b:.1f} PB"

        n = snap.net
        self._net_labels["Send Rate"].configure(text=_rate(n.send_rate_kbps))
        self._net_labels["Recv Rate"].configure(text=_rate(n.recv_rate_kbps))
        self._net_labels["Total Sent"].configure(text=_total(n.bytes_sent))
        self._net_labels["Total Recv"].configure(text=_total(n.bytes_recv))

    def _update_mem(self, snap: DashboardSnapshot) -> None:
        free = snap.ram_total_gb - snap.ram_used_gb
        self._mem_labels["Used"].configure(text=f"{snap.ram_used_gb:.1f} GB")
        self._mem_labels["Free"].configure(text=f"{free:.1f} GB")
        self._mem_labels["Total"].configure(text=f"{snap.ram_total_gb:.1f} GB")
        self._mem_labels["Usage"].configure(text=f"{snap.ram_pct:.1f}%")

    def _update_health(self, health: SystemHealthScore) -> None:
        colors = {
            "Excellent": TOKENS["success"],
            "Good":      TOKENS["accent"],
            "Fair":      TOKENS["warning"],
            "Poor":      TOKENS["error"],
        }
        self._health_badge.configure(
            text=f"Health: {health.total}%  —  {health.label}",
            text_color=colors.get(health.label, TOKENS["text_secondary"]),
        )

    def _on_log_line(self, record) -> None:
        if not self._visible:
            return
        ts   = record.timestamp.strftime("%H:%M:%S")
        line = f"[{ts}]  {record.level:<7}  {record.message}\n"
        self._log_box.configure(state="normal")
        self._log_box.insert("end", line)
        content = self._log_box.get("1.0", "end")
        lines = content.count("\n")
        if lines > 200:
            self._log_box.delete("1.0", "3.0")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")
