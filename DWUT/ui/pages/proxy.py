"""
ui/pages/proxy.py  — Proxy Center

Tabs:
  Generator   — fetch from sources with region/protocol/anonymity filters
  Checker     — 9-stage validation pipeline with live streaming table
  Configure   — chain builder placeholder + output format / export settings
  History     — past fetch/check session summary

No emojis anywhere. Every filter has an (i) badge explaining what it does.
"""

from __future__ import annotations

import threading

import customtkinter as ctk

from core.events import Events, bus
from core.logger import get_logger
from core.worker import worker
from modules.proxy.sources import ProxyRecord, fetch_all_sources
from modules.proxy.checker import CheckResult, check_all
from ui.widgets.smooth_scroll import SmoothScrollFrame
from ui.theme import TOKENS, get_font, card_style, button_style
from ui.widgets.scrolled_table import ScrolledTable
from ui.widgets.tooltip import InfoBadge, attach_tooltip

_log = get_logger(__name__)

_PROTOCOLS  = ["any", "http", "socks4", "socks5"]
_ANONYMITY  = ["any", "elite", "anonymous", "transparent"]
_MAX_PING   = ["any", "50ms", "100ms", "150ms", "250ms", "300ms", "500ms", "750ms", "1000ms", "1500ms", "2000ms"]
_REGIONS    = [
    "Any", "Canada", "United States", "United Kingdom",
    "Germany", "France", "Netherlands", "Switzerland",
    "Japan", "South Korea", "Australia", "Singapore",
    "Brazil", "India", "Russia",
]

_PING_MAP = {"any": 9999, "50ms": 50, "100ms": 100, "150ms": 150, "250ms": 250,
             "300ms": 300, "500ms": 500, "750ms": 750, "1000ms": 1000,
             "1500ms": 1500, "2000ms": 2000}

_EXPORT_FORMATS = [
    "ip:port",
    "protocol://ip:port",
    "ip:port:protocol",
    "Proxifier (*.ppx)",
    "ProxyCap",
    "Shadowsocks JSON",
    "Windows system proxy command",
]

_CHAIN_ROUTING = ["Strict (fail if any hop fails)", "Fail closed", "Automatic"]


class ProxyPage:
    def __init__(self, parent: ctk.CTkFrame) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color=TOKENS["bg_base"], corner_radius=0)
        self._raw_proxies: list[ProxyRecord] = []
        self._results: list[CheckResult] = []
        self._chain_nodes: list[str] = []  # list of proxy addresses in the chain
        self._stop_event: threading.Event | None = None  # set while a check is running
        self._check_running: bool = False
        self._live_stats: dict[str, int] = {}
        self._live_results: list = []   # accumulated CheckResults for the in-progress/last check — lets filters re-render without re-checking
        self._build()
        self._subscribe()

    # ── Top-level layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        # Title + status row
        title_row = ctk.CTkFrame(self.frame, fg_color="transparent", height=52)
        title_row.pack(fill="x", padx=20, pady=(14, 0))
        title_row.pack_propagate(False)

        ctk.CTkLabel(
            title_row, text="Proxy Center",
            font=get_font(18, "bold"), text_color=TOKENS["text_primary"],
        ).pack(side="left", pady=10)

        self._status_label = ctk.CTkLabel(
            title_row, text="",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        )
        self._status_label.pack(side="right", pady=10)

        # Tab bar
        tabs = ["Generator", "Checker", "Configure", "History"]
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

        # Shared progress strip
        self._progress_bar = ctk.CTkProgressBar(
            self.frame, fg_color=TOKENS["bg_input"],
            progress_color=TOKENS["accent"], height=2, corner_radius=0,
        )
        self._progress_bar.set(0)
        self._progress_bar.pack(fill="x")

        self._content = ctk.CTkFrame(self.frame, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(8, 16))

        # Tabs build lazily — only the first-shown tab is constructed now;
        # the rest build on first switch (same fix as OptimizerPage).
        self._tab_builders = {
            "Generator": self._build_generator_tab,
            "Checker":   self._build_checker_tab,
            "Configure": self._build_configure_tab,
            "History":   self._build_history_tab,
        }
        self._built_tabs: set[str] = set()
        self._history_data: list[tuple] = []   # (kind, ts, total, excellent, good, fair) — survives History tab not being built yet

        self._switch_tab("Generator")

    def _switch_tab(self, tab: str) -> None:
        if tab not in self._built_tabs:
            self._tab_builders[tab]()
            self._built_tabs.add(tab)
        for name, frame in self._tab_frames.items():
            frame.pack_forget()
        for name, btn in self._tab_btns.items():
            active = name == tab
            btn.configure(
                text_color=TOKENS["accent"] if active else TOKENS["text_secondary"],
                fg_color=TOKENS["sidebar_active"] if active else "transparent",
            )
        self._tab_frames[tab].pack(fill="both", expand=True)

    # ── Generator tab ──────────────────────────────────────────────────────

    def _build_generator_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Generator"] = f

        # Filter card
        filters = ctk.CTkFrame(f, **card_style())
        filters.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            filters, text="Source Filters",
            font=get_font(12, "bold"), text_color=TOKENS["text_primary"],
        ).pack(anchor="w", padx=14, pady=(10, 6))

        row = ctk.CTkFrame(filters, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 10))

        # Protocol filter
        self._add_filter_group(
            row,
            "Protocol",
            "tip_proto",
            "The proxy protocol to fetch.\n\n"
            "HTTP — basic web proxies\n"
            "SOCKS4 — faster, no auth support\n"
            "SOCKS5 — best: UDP support + authentication\n"
            "'any' fetches all three types.",
            _PROTOCOLS,
            ctk.StringVar(value="any"),
        )

        # Region filter
        self._add_filter_group(
            row,
            "Region",
            "tip_region",
            "Filter proxies by country of their exit IP.\n\n"
            "Applied during the Check phase — the Generator fetches all regions "
            "and the region filter is used when checking to discard proxies "
            "whose geo-IP does not match your selection.",
            _REGIONS,
            ctk.StringVar(value="Any"),
            width=140,
        )

        # Anonymity filter
        self._add_filter_group(
            row,
            "Min Anonymity",
            "tip_anon",
            "The minimum anonymity level to accept.\n\n"
            "Elite       — no identifying headers forwarded to destination\n"
            "Anonymous — some headers stripped but Via/Proxy-Connection present\n"
            "Transparent — your real IP is forwarded (not private)\n\n"
            "'Elite' is recommended for privacy. 'any' accepts all.",
            _ANONYMITY,
            ctk.StringVar(value="any"),
        )

        # Action buttons
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="left", padx=(20, 0))

        self._fetch_btn = ctk.CTkButton(
            actions, text="Fetch Proxies",
            **button_style("primary"), height=34,
            command=self._fetch_proxies,
        )
        self._fetch_btn.pack(pady=(0, 4))

        self._gen_count_label = ctk.CTkLabel(
            actions, text="",
            font=get_font(10), text_color=TOKENS["text_secondary"],
        )
        self._gen_count_label.pack()

        attach_tooltip(
            self._fetch_btn,
            "Downloads proxy lists from 22 public sources in parallel.\n"
            "Deduplicates by IP:port:protocol.\n"
            "Takes 5–15 seconds depending on connection speed."
        )

        # Raw list
        self._raw_box = ctk.CTkTextbox(
            f,
            fg_color=TOKENS["bg_card"],
            text_color=TOKENS["text_secondary"],
            font=get_font(10, mono=True),
            corner_radius=6,
        )
        self._raw_box.pack(fill="both", expand=True, pady=(4, 0))
        self._raw_box.configure(state="disabled")

    def _add_filter_group(
        self, parent, label: str, tip_key: str, tip_text: str,
        choices: list[str], var: ctk.StringVar, width: int = 110,
    ) -> None:
        grp = ctk.CTkFrame(parent, fg_color="transparent")
        grp.pack(side="left", padx=(0, 16))

        hdr = ctk.CTkFrame(grp, fg_color="transparent")
        hdr.pack(anchor="w")

        ctk.CTkLabel(
            hdr, text=label, font=get_font(11),
            text_color=TOKENS["text_secondary"],
        ).pack(side="left")

        InfoBadge(hdr, tip_text).pack(side="left", padx=(4, 0))

        ctk.CTkComboBox(
            grp, values=choices, variable=var,
            fg_color=TOKENS["bg_input"],
            border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"],
            width=width,
        ).pack(pady=(4, 0))

        # store var for later use
        setattr(self, f"_var_{tip_key}", var)

    # ── Checker tab ────────────────────────────────────────────────────────

    def _build_checker_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["Checker"] = f

        # Controls
        ctrl = ctk.CTkFrame(f, **card_style())
        ctrl.pack(fill="x", pady=(0, 8))

        ctrl_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        ctrl_row.pack(fill="x", padx=14, pady=10)

        # Max ping filter
        ping_grp = ctk.CTkFrame(ctrl_row, fg_color="transparent")
        ping_grp.pack(side="left", padx=(0, 16))

        hdr = ctk.CTkFrame(ping_grp, fg_color="transparent")
        hdr.pack(anchor="w")
        ctk.CTkLabel(hdr, text="Max Latency", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        InfoBadge(
            hdr,
            "Maximum acceptable round-trip latency.\n\n"
            "Measured as the time to complete a TCP connection to the proxy port.\n"
            "Proxies slower than this threshold are marked 'rejected'.",
        ).pack(side="left", padx=4)

        self._ping_var = ctk.StringVar(value="any")
        ctk.CTkComboBox(
            ping_grp, values=list(_PING_MAP.keys()), variable=self._ping_var,
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=100,
        ).pack(pady=(4, 0))

        # Custom latency — overrides the preset above if filled in
        custom_grp = ctk.CTkFrame(ctrl_row, fg_color="transparent")
        custom_grp.pack(side="left", padx=(0, 16))

        hdr_custom = ctk.CTkFrame(custom_grp, fg_color="transparent")
        hdr_custom.pack(anchor="w")
        ctk.CTkLabel(hdr_custom, text="Custom Latency (ms)", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        InfoBadge(
            hdr_custom,
            "Type an exact max latency in milliseconds — overrides the preset "
            "dropdown when filled in. Leave blank to use the preset instead.",
        ).pack(side="left", padx=4)

        self._custom_latency_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            custom_grp, textvariable=self._custom_latency_var,
            placeholder_text="e.g. 800",
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=100, height=28, corner_radius=4,
        ).pack(pady=(4, 0))

        # Workers
        workers_grp = ctk.CTkFrame(ctrl_row, fg_color="transparent")
        workers_grp.pack(side="left", padx=(0, 16))

        hdr2 = ctk.CTkFrame(workers_grp, fg_color="transparent")
        hdr2.pack(anchor="w")
        ctk.CTkLabel(hdr2, text="Workers", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        InfoBadge(
            hdr2,
            "Number of proxies checked simultaneously.\n\n"
            "Higher = faster but uses more CPU, RAM, and network sockets.\n"
            "50 is a safe default. Capped at 150 — much higher risks running "
            "out of network resources on some systems. Reduce if you see "
            "high CPU usage or the check behaving oddly.",
        ).pack(side="left", padx=4)

        self._workers_var = ctk.StringVar(value="50")
        ctk.CTkEntry(
            workers_grp, textvariable=self._workers_var,
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=70, height=28, corner_radius=4,
        ).pack(pady=(4, 0))

        # Buttons
        btns = ctk.CTkFrame(ctrl_row, fg_color="transparent")
        btns.pack(side="left", padx=(0, 0))

        self._check_btn = ctk.CTkButton(
            btns, text="Check All",
            **button_style("primary"), height=34,
            command=self._check_proxies,
        )
        attach_tooltip(
            self._check_btn,
            "Runs each proxy through a 9-stage validation pipeline:\n\n"
            "1. Parse — extract IP, port, protocol\n"
            "2. TCP connect — raw socket, measure latency\n"
            "3. Protocol handshake — verify proxy speaks its protocol\n"
            "4. Exit IP test — route through proxy to api.ipify.org\n"
            "5. Transparent check — reject if exit IP matches your real IP\n"
            "6. Geolocation — look up country, ISP, ASN of exit IP\n"
            "7. Anonymity test — check for identifying headers via httpbin\n"
            "8. Pool analysis — detect shared/datacenter infrastructure\n"
            "9. Score 0-100 — classify as Excellent/Good/Fair/Rejected"
        )
        self._check_btn.pack(pady=(0, 4))

        _stop_style = button_style("ghost")
        _stop_style["text_color"] = TOKENS["error"]
        self._stop_btn = ctk.CTkButton(
            btns, text="Stop",
            **_stop_style, height=28, state="disabled",
            command=self._stop_check,
        )
        attach_tooltip(
            self._stop_btn,
            "Forcefully stops the current check.\n\n"
            "No new proxies are started. Anything already mid-check finishes "
            "quietly in the background and is discarded — results collected "
            "so far are kept."
        )
        self._stop_btn.pack()

        ctk.CTkButton(
            btns, text="Export Working",
            **button_style("ghost"), height=28,
            command=self._export_working,
        ).pack()

        # Hide dead/rejected toggle — the checker has to actually check a
        # dead proxy to know it's dead, but there's no reason to clutter
        # the table with thousands of rows nobody's going to use. On by
        # default; results are always in self._live_results/self._results
        # underneath regardless, so stats and export are unaffected.
        hide_grp = ctk.CTkFrame(ctrl_row, fg_color="transparent")
        hide_grp.pack(side="left", padx=(16, 0))
        self._hide_dead_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            hide_grp, text="Hide Dead/Rejected", variable=self._hide_dead_var,
            font=get_font(11), text_color=TOKENS["text_secondary"],
            fg_color=TOKENS["accent"], hover_color=TOKENS["accent_dim"],
            border_color=TOKENS["border"], width=18, height=18,
            command=self._reapply_table_filter,
        ).pack(anchor="w")

        # Stats strip
        self._stats_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        self._stats_row.pack(fill="x", padx=14, pady=(0, 10))

        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        for key, label, color in [
            ("total",     "Total: 0",       TOKENS["text_primary"]),
            ("excellent", "Excellent: 0",   TOKENS["success"]),
            ("good",      "Good: 0",        TOKENS["accent"]),
            ("fair",      "Fair: 0",        TOKENS["warning"]),
            ("rejected",  "Rejected: 0",    TOKENS["error"]),
            ("elite",     "Elite anon: 0",  TOKENS["info"]),
        ]:
            lbl = ctk.CTkLabel(
                self._stats_row, text=label,
                font=get_font(11, "bold"), text_color=color,
            )
            lbl.pack(side="left", padx=10)
            self._stat_labels[key] = lbl

        # Table
        # Fast canvas-based table — no widget creation per row, zero lag
        _cols = [
            ("IP : Port",   150),
            ("Protocol",     66),
            ("Country",      90),
            ("Latency",      64),
            ("Anonymity",    82),
            ("Pool",         80),
            ("Score",        50),
            ("Status",       74),
        ]
        self._table = ScrolledTable(f, columns=_cols)
        self._table.pack(fill="both", expand=True)

    # ── Configure tab ──────────────────────────────────────────────────────

    def _build_configure_tab(self) -> None:
        f = SmoothScrollFrame(self._content, fg_color="transparent", corner_radius=0)
        self._tab_frames["Configure"] = f

        # ── Chain Builder ─────────────────────────────────────────────────
        chain_card = ctk.CTkFrame(f, **card_style())
        chain_card.pack(fill="x", pady=(0, 12))

        hdr = ctk.CTkFrame(chain_card, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(hdr, text="Proxy Chain Builder", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(side="left")
        InfoBadge(
            hdr,
            "A proxy chain routes your traffic through multiple proxies in sequence.\n\n"
            "Example: Your PC -> Canada -> Germany -> Netherlands -> Target\n\n"
            "Each hop adds latency but increases anonymity. Only proxies that passed "
            "the Checker with 'Good' or better are eligible to add to a chain.",
        ).pack(side="left", padx=6)

        ctk.CTkLabel(
            chain_card,
            text="Add proxies from the Checker results by running a check first,\n"
                 "then select working proxies to build a chain.",
            font=get_font(11), text_color=TOKENS["text_secondary"],
        ).pack(anchor="w", padx=14, pady=(0, 6))

        # Chain node display
        self._chain_display = ctk.CTkFrame(
            chain_card, fg_color=TOKENS["bg_input"],
            corner_radius=6, height=60,
        )
        self._chain_display.pack(fill="x", padx=14, pady=(0, 8))
        self._chain_display.pack_propagate(False)

        self._chain_label = ctk.CTkLabel(
            self._chain_display,
            text="No chain configured — check proxies first, then add nodes below.",
            font=get_font(10), text_color=TOKENS["text_disabled"],
        )
        self._chain_label.pack(expand=True)

        btn_row = ctk.CTkFrame(chain_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 10))

        for label, cmd, variant, tip in [
            ("Add Node",      self._chain_add_node,   "ghost",
             "Add the best available checked proxy to this chain."),
            ("Clear Chain",   self._chain_clear,      "ghost",
             "Remove all nodes from the chain."),
            ("Test Chain",    self._chain_test,       "primary",
             "Send a test request through the full chain to verify it works."),
            ("Export Chain",  self._chain_export,     "ghost",
             "Copy the chain configuration to the clipboard or save to file."),
        ]:
            b = ctk.CTkButton(btn_row, text=label, **button_style(variant), height=30, command=cmd)
            attach_tooltip(b, tip)
            b.pack(side="left", padx=4)

        # Routing options
        routing_card = ctk.CTkFrame(chain_card, fg_color=TOKENS["bg_input"], corner_radius=6)
        routing_card.pack(fill="x", padx=14, pady=(0, 12))

        routing_row = ctk.CTkFrame(routing_card, fg_color="transparent")
        routing_row.pack(fill="x", padx=12, pady=8)

        hdr2 = ctk.CTkFrame(routing_row, fg_color="transparent")
        hdr2.pack(side="left")
        ctk.CTkLabel(hdr2, text="Routing:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        InfoBadge(
            hdr2,
            "Strict: fail immediately if any hop in the chain fails.\n"
            "Fail closed: stop traffic if chain breaks (security-focused).\n"
            "Automatic: try alternative paths if a hop fails.",
        ).pack(side="left", padx=4)

        self._routing_var = ctk.StringVar(value=_CHAIN_ROUTING[0])
        ctk.CTkComboBox(
            routing_row, values=_CHAIN_ROUTING, variable=self._routing_var,
            fg_color=TOKENS["bg_card"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=240,
        ).pack(side="left", padx=12)

        dns_row = ctk.CTkFrame(routing_card, fg_color="transparent")
        dns_row.pack(fill="x", padx=12, pady=(0, 8))

        hdr3 = ctk.CTkFrame(dns_row, fg_color="transparent")
        hdr3.pack(side="left")
        ctk.CTkLabel(hdr3, text="DNS through proxy:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")
        InfoBadge(
            hdr3,
            "Route DNS lookups through the proxy chain instead of your local DNS.\n\n"
            "Enabled: prevents DNS leaks — your ISP cannot see which sites you visit.\n"
            "Disabled: faster but DNS queries bypass the proxy."
        ).pack(side="left", padx=4)

        self._dns_proxy_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            dns_row, text="", variable=self._dns_proxy_var,
            fg_color=TOKENS["bg_card"], progress_color=TOKENS["accent"],
            button_color=TOKENS["accent"], width=40,
        ).pack(side="left", padx=12)

        # ── Export Format ─────────────────────────────────────────────────
        export_card = ctk.CTkFrame(f, **card_style())
        export_card.pack(fill="x", pady=(0, 12))

        hdr4 = ctk.CTkFrame(export_card, fg_color="transparent")
        hdr4.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(hdr4, text="Export Format", font=get_font(13, "bold"),
                     text_color=TOKENS["text_primary"]).pack(side="left")
        InfoBadge(
            hdr4,
            "Choose the format for exporting your checked proxy list.\n\n"
            "ip:port is the simplest and works with most tools.\n"
            "Proxifier and ProxyCap formats can be imported directly into those apps.\n"
            "Shadowsocks JSON is used by shadowsocks-ng / Clash / v2ray."
        ).pack(side="left", padx=6)

        fmt_row = ctk.CTkFrame(export_card, fg_color="transparent")
        fmt_row.pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(fmt_row, text="Format:", font=get_font(11),
                     text_color=TOKENS["text_secondary"]).pack(side="left")

        self._export_fmt_var = ctk.StringVar(value=_EXPORT_FORMATS[0])
        ctk.CTkComboBox(
            fmt_row, values=_EXPORT_FORMATS, variable=self._export_fmt_var,
            fg_color=TOKENS["bg_input"], border_color=TOKENS["border"],
            text_color=TOKENS["text_primary"], width=240,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            fmt_row, text="Export List",
            **button_style("primary"), height=30,
            command=self._export_working,
        ).pack(side="left", padx=8)

        # ── Checker Settings ──────────────────────────────────────────────
        check_card = ctk.CTkFrame(f, **card_style())
        check_card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(check_card, text="Checker Settings",
                     font=get_font(13, "bold"), text_color=TOKENS["text_primary"]
                     ).pack(anchor="w", padx=14, pady=(12, 6))

        settings = [
            ("Verify exit IP through proxy",    "check_exit_ip",
             "Route a request through the proxy to confirm the exit IP differs from yours.\n"
             "Detects transparent proxies. Adds ~2s per proxy to check time."),
            ("Test anonymity headers",          "check_anon_headers",
             "Send a request to httpbin.org/headers through the proxy and check if "
             "identifying headers (X-Forwarded-For, Via, etc.) are present.\n"
             "Adds ~3s per proxy but is the only reliable way to detect elite vs anonymous."),
            ("Detect pooled/datacenter proxies","check_pool",
             "Flag proxies on known datacenter ASNs (AWS, Azure, Hetzner, OVH, etc.).\n"
             "These are often blocked by sites that detect hosting provider IPs."),
            ("Multi-request pool detection",    "check_multi_req",
             "Send 3 requests through the same proxy and compare exit IPs.\n"
             "If they differ, the proxy is confirmed to use a shared pool.\n"
             "Adds ~5s per proxy but is the most reliable pool detection method."),
        ]

        self._checker_setting_vars: dict[str, ctk.BooleanVar] = {}

        for label, key, tip in settings:
            row = ctk.CTkFrame(check_card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=3)

            hdr_r = ctk.CTkFrame(row, fg_color="transparent")
            hdr_r.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(hdr_r, text=label, font=get_font(11),
                         text_color=TOKENS["text_secondary"], anchor="w").pack(side="left")
            InfoBadge(hdr_r, tip).pack(side="left", padx=4)

            var = ctk.BooleanVar(value=True)
            self._checker_setting_vars[key] = var
            ctk.CTkSwitch(
                row, text="", variable=var,
                fg_color=TOKENS["bg_input"], progress_color=TOKENS["accent"],
                button_color=TOKENS["accent"], width=40,
            ).pack(side="right")

        ctk.CTkFrame(check_card, fg_color="transparent", height=8).pack()

    # ── History tab ────────────────────────────────────────────────────────

    def _build_history_tab(self) -> None:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tab_frames["History"] = f

        ctk.CTkLabel(
            f, text="Session History",
            font=get_font(14, "bold"), text_color=TOKENS["text_primary"],
        ).pack(anchor="w", pady=(0, 10))

        self._history_scroll = SmoothScrollFrame(
            f, fg_color=TOKENS["bg_card"], corner_radius=6
        )
        self._history_scroll.pack(fill="both", expand=True)

        # Render any entries that arrived before this tab was ever built —
        # tabs are now lazy (only built on first visit), but a fetch/check
        # can finish before the user has ever clicked into History.
        if self._history_data:
            for kind, ts, total, excellent, good, fair in self._history_data:
                self._render_history_row(kind, ts, total, excellent, good, fair)
        else:
            empty_label = ctk.CTkLabel(
                self._history_scroll,
                text="No sessions yet. Fetch and check proxies to see history here.",
                font=get_font(11), text_color=TOKENS["text_disabled"],
            )
            empty_label.pack(pady=30)
            empty_label._is_empty_state = True

    # ── EventBus subscriptions ─────────────────────────────────────────────

    def _subscribe(self) -> None:
        bus.subscribe(Events.PROXY_FETCH_PROGRESS, self._on_fetch_progress)
        bus.subscribe(Events.PROXY_FETCH_DONE,     self._on_fetch_done)
        bus.subscribe(Events.PROXY_CHECK_PROGRESS, self._on_check_progress)
        bus.subscribe(Events.PROXY_CHECK_DONE,     self._on_check_done)
        bus.subscribe(Events.PROXY_RESULT_BATCH,   self._on_proxy_result_batch)

    def _on_fetch_progress(self, data: dict) -> None:
        pct = data["completed"] / max(data["total"], 1)
        self._progress_bar.set(pct)
        self._status_label.configure(
            text=f"Fetching {data['completed']}/{data['total']}  —  {data['running_total']} found"
        )

    def _on_fetch_done(self, proxies: list) -> None:
        self._raw_proxies = proxies
        self._fetch_btn.configure(state="normal")
        self._progress_bar.set(1.0)
        n = len(proxies)
        self._gen_count_label.configure(text=f"{n:,} proxies fetched")
        self._status_label.configure(text=f"Fetch done  —  {n:,} unique proxies")

        self._raw_box.configure(state="normal")
        self._raw_box.delete("1.0", "end")
        for p in proxies[:3000]:
            self._raw_box.insert("end", f"{p.protocol}://{p.ip}:{p.port}\n")
        if len(proxies) > 3000:
            self._raw_box.insert("end", f"... {len(proxies)-3000} more (showing first 3000)\n")
        self._raw_box.configure(state="disabled")

        self._add_history_entry("Fetch", n, 0, 0, 0)

    def _on_check_progress(self, data: dict) -> None:
        pct = data["completed"] / max(data["total"], 1)
        self._progress_bar.set(pct)
        self._status_label.configure(
            text=f"Checking {data['completed']}/{data['total']}  "
                 f"—  last: {data['last_proxy']}  [{data['last_tier']}]"
        )

    def _on_check_done(self, results: list) -> None:
        self._results = results
        self._check_running = False
        self._stop_event = None
        self._check_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._progress_bar.set(1.0)

        excellent = sum(1 for r in results if r.tier == "excellent")
        good      = sum(1 for r in results if r.tier == "good")
        fair      = sum(1 for r in results if r.tier == "fair")
        rejected  = sum(1 for r in results if r.tier in ("rejected", "dead"))
        elite     = sum(1 for r in results if r.anonymity == "elite" and r.tier not in ("dead",))

        self._stat_labels["total"].configure(text=f"Total: {len(results):,}")
        self._stat_labels["excellent"].configure(text=f"Excellent: {excellent}")
        self._stat_labels["good"].configure(text=f"Good: {good}")
        self._stat_labels["fair"].configure(text=f"Fair: {fair}")
        self._stat_labels["rejected"].configure(text=f"Rejected: {rejected}")
        self._stat_labels["elite"].configure(text=f"Elite anon: {elite}")

        working = excellent + good + fair
        was_stopped = len(results) < len(self._raw_proxies)
        suffix = "  (stopped early)" if was_stopped else ""
        self._status_label.configure(
            text=f"Check done  —  {working}/{len(results)} working{suffix}"
        )
        self._add_history_entry("Check", len(results), excellent, good, fair)

    def _row_for_result(self, result: CheckResult) -> tuple[list[str], str]:
        pool_text = "Likely pooled" if result.pool_flags else "Appears unique"
        cells = [
            result.proxy.address(),
            result.proxy.protocol.upper(),
            result.country or "—",
            f"{result.tcp_latency_ms:.0f} ms" if result.tcp_latency_ms else "—",
            result.anonymity or "—",
            pool_text,
            str(result.score),
            result.tier.upper(),
        ]
        return cells, result.tier

    def _reapply_table_filter(self) -> None:
        """Rebuilds the visible table from self._live_results against the
        current Hide Dead/Rejected setting — no re-checking needed, all
        results are already known."""
        hide_dead = self._hide_dead_var.get()
        rows = [
            self._row_for_result(r) for r in self._live_results
            if not (hide_dead and r.tier in ("dead", "rejected"))
        ]
        self._table.set_rows(rows)

    def _on_proxy_result_batch(self, batch: list[CheckResult]) -> None:
        """
        Handles a batch of newly-completed results. check_all() itself only
        publishes a batch roughly every 200ms (not one event per proxy), so
        there's no need to re-buffer on this side too — just render what
        arrived.
        """
        if not batch:
            return

        self._live_results.extend(batch)

        hide_dead = self._hide_dead_var.get()
        rows_data = []
        for result in batch:
            self._live_stats[result.tier] = self._live_stats.get(result.tier, 0) + 1
            if result.anonymity == "elite" and result.tier != "dead":
                self._live_stats["elite"] = self._live_stats.get("elite", 0) + 1

            if hide_dead and result.tier in ("dead", "rejected"):
                continue   # still counted above, just not shown — that's the point
            rows_data.append(self._row_for_result(result))

        # One redraw for the whole batch, not one per row
        if rows_data:
            self._table.append_rows(rows_data)

        # Keep the stats strip live while a large check is still running,
        # instead of sitting at "0" until the entire batch finishes
        if self._check_running:
            total_done = sum(v for k, v in self._live_stats.items() if k != "elite")
            self._stat_labels["total"].configure(text=f"Total: {total_done:,}")
            self._stat_labels["excellent"].configure(text=f"Excellent: {self._live_stats.get('excellent', 0)}")
            self._stat_labels["good"].configure(text=f"Good: {self._live_stats.get('good', 0)}")
            self._stat_labels["fair"].configure(text=f"Fair: {self._live_stats.get('fair', 0)}")
            rejected = self._live_stats.get("rejected", 0) + self._live_stats.get("dead", 0)
            self._stat_labels["rejected"].configure(text=f"Rejected: {rejected}")
            self._stat_labels["elite"].configure(text=f"Elite anon: {self._live_stats.get('elite', 0)}")

    # ── Actions ────────────────────────────────────────────────────────────

    def _fetch_proxies(self) -> None:
        self._fetch_btn.configure(state="disabled")
        self._progress_bar.set(0)
        self._gen_count_label.configure(text="")
        self._status_label.configure(text="Starting fetch...")
        self._raw_box.configure(state="normal")
        self._raw_box.delete("1.0", "end")
        self._raw_box.configure(state="disabled")
        worker.submit(fetch_all_sources)

    def _check_proxies(self) -> None:
        if not self._raw_proxies:
            self._status_label.configure(text="Fetch proxies first.")
            return

        # Clear table and stats
        self._table.clear()
        self._live_stats.clear()
        self._live_results.clear()
        for lbl in self._stat_labels.values():
            lbl.configure(text=lbl.cget("text").split(":")[0] + ": 0")

        self._check_running = True
        self._stop_event = threading.Event()
        self._check_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress_bar.set(0)

        try:
            workers = int(self._workers_var.get())
        except ValueError:
            workers = 50
        workers = max(1, min(workers, 150))

        # Filters — protocol/region/anonymity come from the Generator tab's
        # filter row (they used to be collected but never actually passed
        # to the checker, so they looked like they did nothing). Max
        # latency comes from this tab's own preset dropdown, or the custom
        # field if the user typed one (which wins over the preset).
        protocol_filter = getattr(self, "_var_tip_proto", None)
        protocol_filter = protocol_filter.get() if protocol_filter else "any"

        region_filter = getattr(self, "_var_tip_region", None)
        region_filter = region_filter.get() if region_filter else "Any"

        min_anonymity = getattr(self, "_var_tip_anon", None)
        min_anonymity = min_anonymity.get() if min_anonymity else "any"

        max_latency_ms: Optional[float] = None
        custom_text = self._custom_latency_var.get().strip()
        if custom_text:
            try:
                max_latency_ms = float(custom_text)
            except ValueError:
                max_latency_ms = None
        if max_latency_ms is None:
            preset = _PING_MAP.get(self._ping_var.get(), 9999)
            if preset < 9999:
                max_latency_ms = float(preset)

        proxies = list(self._raw_proxies)
        worker.submit(
            check_all, proxies,
            max_workers=workers, stop_event=self._stop_event,
            region_filter=region_filter, protocol_filter=protocol_filter,
            min_anonymity=min_anonymity, max_latency_ms=max_latency_ms,
            on_error=lambda e: bus.publish(Events.APP_ERROR, f"Check failed: {e}"),
        )

    def _stop_check(self) -> None:
        if not self._stop_event:
            return
        self._stop_event.set()
        self._stop_btn.configure(state="disabled")
        self._status_label.configure(text="Stopping check…")

    def _export_working(self) -> None:
        import tkinter as tk
        working = [r for r in self._results if r.tier in ("excellent", "good", "fair")]
        if not working:
            self._status_label.configure(text="No working proxies to export — run Check first.")
            return

        fmt = self._export_fmt_var.get()
        lines: list[str] = []

        for r in working:
            p = r.proxy
            if fmt == "ip:port":
                lines.append(f"{p.ip}:{p.port}")
            elif fmt == "protocol://ip:port":
                lines.append(f"{p.protocol}://{p.ip}:{p.port}")
            elif fmt == "ip:port:protocol":
                lines.append(f"{p.ip}:{p.port}:{p.protocol}")
            else:
                lines.append(f"{p.protocol}://{p.ip}:{p.port}")

        text = "\n".join(lines)
        self.frame.clipboard_clear()
        self.frame.clipboard_append(text)
        self._status_label.configure(
            text=f"Copied {len(lines)} proxies to clipboard in '{fmt}' format"
        )

    # ── Chain helpers ──────────────────────────────────────────────────────

    def _chain_add_node(self) -> None:
        best = [r for r in self._results if r.tier in ("excellent", "good")]
        if not best:
            self._status_label.configure(text="No good proxies available — run Check first.")
            return
        # Pick first unused
        used = set(self._chain_nodes)
        for r in best:
            addr = r.proxy.address()
            if addr not in used:
                self._chain_nodes.append(addr)
                self._update_chain_display()
                return
        self._status_label.configure(text="All good proxies already added to chain.")

    def _chain_clear(self) -> None:
        self._chain_nodes.clear()
        self._update_chain_display()

    def _chain_test(self) -> None:
        if not self._chain_nodes:
            self._status_label.configure(text="Add nodes to the chain first.")
            return
        self._status_label.configure(text="Chain test — not yet implemented in this build.")

    def _chain_export(self) -> None:
        if not self._chain_nodes:
            return
        text = "\n".join(self._chain_nodes)
        self.frame.clipboard_clear()
        self.frame.clipboard_append(text)
        self._status_label.configure(text=f"Chain ({len(self._chain_nodes)} nodes) copied to clipboard.")

    def _update_chain_display(self) -> None:
        for w in self._chain_display.winfo_children():
            w.destroy()

        if not self._chain_nodes:
            self._chain_label = ctk.CTkLabel(
                self._chain_display,
                text="No chain configured.",
                font=get_font(10), text_color=TOKENS["text_disabled"],
            )
            self._chain_label.pack(expand=True)
            return

        row = ctk.CTkFrame(self._chain_display, fg_color="transparent")
        row.pack(expand=True)

        for i, node in enumerate(self._chain_nodes):
            ctk.CTkLabel(
                row, text=node,
                font=get_font(10, "bold"), text_color=TOKENS["accent"],
            ).pack(side="left")
            if i < len(self._chain_nodes) - 1:
                ctk.CTkLabel(
                    row, text=" -> ",
                    font=get_font(10), text_color=TOKENS["text_disabled"],
                ).pack(side="left")

    # ── History helpers ────────────────────────────────────────────────────

    def _add_history_entry(
        self, kind: str, total: int, excellent: int, good: int, fair: int
    ) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")

        # Always record the data — the History tab may not have been built
        # yet (tabs are lazy, only built on first visit), so this can't
        # depend on self._history_scroll existing.
        self._history_data.append((kind, ts, total, excellent, good, fair))

        if not hasattr(self, "_history_scroll"):
            return   # tab not built yet — _build_history_tab will replay _history_data

        self._render_history_row(kind, ts, total, excellent, good, fair)

    def _render_history_row(
        self, kind: str, ts: str, total: int, excellent: int, good: int, fair: int
    ) -> None:
        # Clear "no sessions" label on first entry
        for w in self._history_scroll.winfo_children():
            if getattr(w, "_is_empty_state", False):
                w.destroy()

        row = ctk.CTkFrame(
            self._history_scroll,
            fg_color=TOKENS["bg_input"], corner_radius=4,
        )
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(
            row, text=ts, font=get_font(10),
            text_color=TOKENS["text_disabled"], width=70, anchor="w",
        ).pack(side="left", padx=8, pady=6)

        ctk.CTkLabel(
            row, text=kind, font=get_font(11, "bold"),
            text_color=TOKENS["text_primary"], width=60, anchor="w",
        ).pack(side="left")

        summary = f"{total:,} proxies"
        if kind == "Check":
            working = excellent + good + fair
            summary += f"  |  {working} working ({excellent} excellent, {good} good, {fair} fair)"

        ctk.CTkLabel(
            row, text=summary, font=get_font(11),
            text_color=TOKENS["text_secondary"], anchor="w",
        ).pack(side="left", padx=8)

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass
