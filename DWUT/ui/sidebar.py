"""
ui/sidebar.py — Collapsible navigation rail. No emojis.

200px expanded, 48px icon-only collapsed.
Active item gets accent left-border + accent text.
"""

from __future__ import annotations

from typing import Callable
import customtkinter as ctk
import tkinter as tk

from app.config import config
from app.state import state
from ui.theme import TOKENS, get_font

# Nav items: (page_key, display_label, icon_char)
# ASCII/unicode text icons — no emoji
_NAV_ITEMS = [
    ("dashboard", "Dashboard",    "D"),
    ("optimizer", "Optimizer",    "O"),
    ("gaming",    "Gaming",       "G"),
    ("debloat",   "Debloat",      "B"),
    ("proxy",     "Proxy Center", "P"),
    ("tools",     "Tools",        "T"),
    ("settings",  "Settings",     "S"),
]

_W_EXPANDED  = 200
_W_COLLAPSED = 52


class Sidebar:
    def __init__(self, parent: ctk.CTkFrame, on_navigate: Callable[[str], None]) -> None:
        self._on_navigate = on_navigate
        self._collapsed = config.sidebar_collapsed
        self._active_page = ""
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._active_border: dict[str, ctk.CTkFrame] = {}  # accent left-border per item

        self.frame = ctk.CTkFrame(
            parent,
            fg_color=TOKENS["sidebar_bg"],
            corner_radius=0,
            width=_W_COLLAPSED if self._collapsed else _W_EXPANDED,
        )
        self.frame.pack_propagate(False)
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Logo / wordmark area
        logo_area = ctk.CTkFrame(self.frame, fg_color="transparent", height=58)
        logo_area.pack(fill="x")
        logo_area.pack_propagate(False)

        if self._collapsed:
            # No native titlebar anymore (see MainWindow), so the collapsed
            # icon-only rail is the only place a "what app is this" cue
            # exists at all — show a small DWUT mark, stacked above the
            # toggle since there's no room to sit beside it at 52px wide.
            ctk.CTkLabel(
                logo_area, text="DWUT",
                font=get_font(9, "bold"),
                text_color=TOKENS["text_primary"],
            ).pack(pady=(8, 0))
            toggle = ctk.CTkButton(
                logo_area,
                text=">",
                width=28, height=28,
                fg_color="transparent",
                hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_secondary"],
                font=get_font(13, "bold"),
                corner_radius=6,
                command=self._toggle_collapse,
            )
            toggle.pack(pady=(2, 0))
        else:
            # Expanded — with the native titlebar gone, this is the only
            # place the full app name appears at all, so show it (wrapped —
            # "Darko's Windows Utility Tool" doesn't fit on one line at a
            # readable size in a 200px-wide rail).
            ctk.CTkLabel(
                logo_area, text="Darko's Windows Utility Tool",
                font=get_font(10, "bold"),
                text_color=TOKENS["text_primary"],
                anchor="w", justify="left",
                wraplength=128,
            ).pack(side="left", padx=(14, 2), pady=8, fill="y")
            toggle = ctk.CTkButton(
                logo_area,
                text="<",
                width=28, height=28,
                fg_color="transparent",
                hover_color=TOKENS["bg_hover"],
                text_color=TOKENS["text_secondary"],
                font=get_font(13, "bold"),
                corner_radius=6,
                command=self._toggle_collapse,
            )
            toggle.pack(side="right", padx=8, pady=15)

        # Divider
        ctk.CTkFrame(self.frame, fg_color=TOKENS["border"], height=1).pack(fill="x")

        # Nav items
        nav_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        nav_frame.pack(fill="both", expand=True, pady=10)

        for page_key, label, icon in _NAV_ITEMS:
            self._make_item(nav_frame, page_key, label, icon)

        # Bottom section — admin status + version
        ctk.CTkFrame(self.frame, fg_color=TOKENS["border"], height=1).pack(fill="x")

        bottom = ctk.CTkFrame(self.frame, fg_color="transparent")
        bottom.pack(fill="x", pady=10, padx=10)

        if not self._collapsed:
            admin_txt   = "Administrator" if state.is_admin else "No Admin Rights"
            admin_color = TOKENS["success"] if state.is_admin else TOKENS["warning"]
            ctk.CTkLabel(
                bottom,
                text=admin_txt,
                font=get_font(10),
                text_color=admin_color,
                anchor="w",
            ).pack(fill="x")
        else:
            dot_color = TOKENS["success"] if state.is_admin else TOKENS["warning"]
            ctk.CTkLabel(
                bottom,
                text="A" if state.is_admin else "!",
                font=get_font(10, "bold"),
                text_color=dot_color,
                anchor="center",
            ).pack()

    def _make_item(self, parent: ctk.CTkFrame, key: str, label: str, icon: str) -> None:
        """
        Each nav item is a row: a 3px accent left-border + the button.
        The border is hidden when inactive and shown (accent color) when active.
        """
        row = ctk.CTkFrame(parent, fg_color="transparent", height=40)
        row.pack(fill="x", pady=2)
        row.pack_propagate(False)

        # Accent left border — 3px wide, full height
        border = ctk.CTkFrame(row, fg_color="transparent", width=3)
        border.pack(side="left", fill="y")
        self._active_border[key] = border

        if self._collapsed:
            btn_text = icon
            btn_w    = _W_COLLAPSED - 3
            anchor   = "center"
            padx     = 0
        else:
            btn_text = f"  {icon}    {label}"
            btn_w    = _W_EXPANDED - 3
            anchor   = "w"
            padx     = 4

        btn = ctk.CTkButton(
            row,
            text=btn_text,
            anchor=anchor,
            height=36,
            width=btn_w,
            fg_color="transparent",
            hover_color=TOKENS["bg_hover"],
            text_color=TOKENS["sidebar_icon_rest"],
            font=get_font(12),
            corner_radius=4,
            command=lambda k=key: self._on_navigate(k),
        )
        btn.pack(side="left", padx=(0, padx))
        self._buttons[key] = btn

    # ── Active state ───────────────────────────────────────────────────────

    def set_active(self, page_key: str) -> None:
        # Reset previous
        if self._active_page and self._active_page in self._buttons:
            self._buttons[self._active_page].configure(
                fg_color="transparent",
                text_color=TOKENS["sidebar_icon_rest"],
            )
            if self._active_page in self._active_border:
                self._active_border[self._active_page].configure(fg_color="transparent")

        self._active_page = page_key

        if page_key in self._buttons:
            self._buttons[page_key].configure(
                fg_color=TOKENS["sidebar_active"],
                text_color=TOKENS["sidebar_icon_active"],
            )
        if page_key in self._active_border:
            self._active_border[page_key].configure(
                fg_color=TOKENS["sidebar_active_border"]
            )

    # ── Collapse ───────────────────────────────────────────────────────────

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        config.sidebar_collapsed = self._collapsed
        config.save()

        self.frame.configure(width=_W_COLLAPSED if self._collapsed else _W_EXPANDED)
        for w in self.frame.winfo_children():
            w.destroy()
        self._buttons.clear()
        self._active_border.clear()
        self._build()
        if self._active_page:
            self.set_active(self._active_page)
