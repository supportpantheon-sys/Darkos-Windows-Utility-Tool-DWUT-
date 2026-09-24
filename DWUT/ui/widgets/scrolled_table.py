"""
ui/widgets/scrolled_table.py

A fast VIRTUALIZED table widget built on tk.Canvas + tk.Scrollbar.

Why not CTkScrollableFrame?
  CTkScrollableFrame creates real child widgets and re-packs them on every
  scroll event. When you add hundreds of rows it causes heavy layout
  recalculation and visible flicker / lag.

Why not just draw every row on a Canvas (the old approach)?
  That avoids widget creation, but it still creates 3+ canvas items per row
  up front. At proxy-checker scale (tens of thousands of rows) that's
  60,000+ live canvas items, which makes every scroll, resize, and drag
  event slow (Tk has to walk the whole item list) — this was the real
  cause of the Checker tab feeling laggy on large proxy lists.

This widget instead:
  - Stores row data only (cheap python list, no canvas items)
  - Draws ONLY the rows currently in the visible viewport (+small buffer)
  - Redraws that small window on scroll / resize, so cost never scales
    with total row count — checking 20,000 proxies costs the same to
    render as checking 20
  - Supports appending in batches (append_rows) so a flush of hundreds of
    buffered results is one redraw, not hundreds
  - Has both vertical AND horizontal scrollbars, so columns are never
    silently clipped off-screen on a narrower window

Usage:
    cols = [("IP:Port", 160), ("Protocol", 70), ("Status", 80)]
    tbl = ScrolledTable(parent, columns=cols)
    tbl.pack(fill="both", expand=True)

    tbl.append_row(["1.2.3.4:8080", "SOCKS5", "OK"], tag="ok")
    tbl.append_rows([(cells1, "ok"), (cells2, "dead")])   # batch — prefer this
    tbl.clear()

Tags map to row colors:
    "excellent" -> green text
    "good"      -> accent text
    "fair"      -> warning text
    "rejected"  -> error text
    "dead"      -> disabled text
    ""          -> primary text (default)
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Optional

from ui.theme import TOKENS, get_font


_ROW_H    = 26    # pixels per row
_HEADER_H = 28    # pixels for header
_BUFFER_ROWS = 4  # extra rows drawn above/below the viewport so fast scrolls don't flash blank


class ScrolledTable(tk.Frame):
    """
    Virtualized canvas-based table. Only ever draws the visible rows.
    Handles tens of thousands of rows without lag.
    """

    _TAG_COLORS = {
        "excellent": None,   # set from TOKENS in __init__
        "good":      None,
        "fair":      None,
        "rejected":  None,
        "dead":      None,
        "":          None,
    }

    def __init__(
        self,
        parent,
        columns: list[tuple[str, int]],   # (header_text, width_px)
        **kwargs,
    ) -> None:
        bg = TOKENS["bg_card"]
        super().__init__(parent, bg=bg, **kwargs)

        self._columns = columns
        self._rows: list[tuple[list[str], str]] = []   # (cells, tag)
        self._total_w = sum(w for _, w in columns) + 8
        self._autoscroll = True     # follow new rows while scrolled to bottom
        self._first_drawn = -1      # currently-drawn window, so redraws can short-circuit
        self._last_drawn = -1

        # Resolve tag colors from current TOKENS
        self._colors = {
            "excellent": TOKENS["success"],
            "good":      TOKENS["accent"],
            "fair":      TOKENS["warning"],
            "rejected":  TOKENS["error"],
            "dead":      TOKENS["text_disabled"],
            "":          TOKENS["text_primary"],
        }
        self._alt_bg  = [TOKENS["bg_input"], TOKENS["bg_card"]]
        self._hdr_bg  = TOKENS["bg_input"]
        self._hdr_fg  = TOKENS["text_secondary"]
        self._border  = TOKENS["border"]

        # Font for header + body text — must exist before _draw_header() runs
        f = get_font(10)
        self._font = tkfont.Font(family=f[0], size=f[1])

        # Header canvas (fixed, not scrolled vertically — but tracks horizontal scroll)
        self._hdr = tk.Canvas(
            self, bg=self._hdr_bg, height=_HEADER_H,
            highlightthickness=0, bd=0,
        )
        self._hdr.pack(fill="x")
        self._draw_header()

        # Body canvas + scrollbars
        body_frame = tk.Frame(self, bg=bg)
        body_frame.pack(fill="both", expand=True)

        self._vsb = tk.Scrollbar(body_frame, orient="vertical", bg=TOKENS["bg_input"])
        self._vsb.pack(side="right", fill="y")

        self._hsb = tk.Scrollbar(self, orient="horizontal", bg=TOKENS["bg_input"])
        self._hsb.pack(side="bottom", fill="x")

        self._canvas = tk.Canvas(
            body_frame,
            bg=bg,
            highlightthickness=0,
            bd=0,
            yscrollcommand=self._vsb.set,
            xscrollcommand=self._hsb.set,
        )
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vsb.config(command=self._on_vscroll)
        self._hsb.config(command=self._on_hscroll)

        # Bind scroll wheel (vertical; shift+wheel = horizontal, standard convention)
        self._canvas.bind("<MouseWheel>",       self._on_mousewheel)
        self._canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_h)
        self._canvas.bind("<Button-4>",         self._on_mousewheel)
        self._canvas.bind("<Button-5>",         self._on_mousewheel)
        self._canvas.bind("<Configure>",        self._on_resize)

        self._update_scroll_region()

    # ── Header ────────────────────────────────────────────────────────────

    def _draw_header(self, x_offset: int = 0) -> None:
        self._hdr.delete("all")
        x = 6 - x_offset
        for text, w in self._columns:
            self._hdr.create_text(
                x + 4, _HEADER_H // 2,
                text=text, anchor="w",
                fill=self._hdr_fg,
                font=self._font,
            )
            x += w
        # bottom border line
        self._hdr.create_line(0, _HEADER_H - 1, self._total_w, _HEADER_H - 1,
                               fill=self._border)

    # ── Public API ────────────────────────────────────────────────────────

    def append_row(self, cells: list[str], tag: str = "") -> None:
        self.append_rows([(cells, tag)])

    def append_rows(self, rows: list[tuple[list[str], str]]) -> None:
        """Append a batch of rows in a single redraw pass (use this over
        looping append_row — one geometry/scroll update no matter how many
        rows come in)."""
        if not rows:
            return
        self._rows.extend(rows)
        self._update_scroll_region()
        if self._autoscroll:
            self._canvas.yview_moveto(1.0)
        self._redraw_visible(force=True)

    def clear(self) -> None:
        self._rows.clear()
        self._canvas.delete("all")
        self._first_drawn = -1
        self._last_drawn = -1
        self._autoscroll = True
        self._canvas.yview_moveto(0.0)
        self._update_scroll_region()

    def set_rows(self, rows: list[tuple[list[str], str]]) -> None:
        """Replace all rows at once."""
        self._rows = rows
        self._first_drawn = -1
        self._last_drawn = -1
        self._update_scroll_region()
        self._redraw_visible(force=True)

    def row_count(self) -> int:
        return len(self._rows)

    # ── Scrolling (vertical drives virtualization; horizontal just pans) ────

    def _on_vscroll(self, *args) -> None:
        # User grabbed the scrollbar — they're taking manual control
        self._autoscroll = False
        self._canvas.yview(*args)
        self._redraw_visible()

    def _on_hscroll(self, *args) -> None:
        self._canvas.xview(*args)
        self._sync_header_scroll()

    def _on_mousewheel(self, event: tk.Event) -> None:
        self._autoscroll = False
        if getattr(event, "delta", 0):
            self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        elif event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        # Re-enable autoscroll if the wheel lands us back at the bottom
        top, bottom = self._canvas.yview()
        if bottom >= 0.999:
            self._autoscroll = True
        self._redraw_visible()

    def _on_mousewheel_h(self, event: tk.Event) -> None:
        if getattr(event, "delta", 0):
            self._canvas.xview_scroll(-1 * (event.delta // 120), "units")
        self._sync_header_scroll()

    def _sync_header_scroll(self) -> None:
        left, _right = self._canvas.xview()
        x_offset = int(left * self._total_w)
        self._draw_header(x_offset)

    # ── Drawing (virtualized — only the visible window ever has items) ─────

    def _redraw_visible(self, force: bool = False) -> None:
        self._canvas.update_idletasks()
        view_h = max(self._canvas.winfo_height(), 1)
        top_frac, bottom_frac = self._canvas.yview()
        total_h = max(len(self._rows) * _ROW_H, 1)

        first_row = max(0, int((top_frac * total_h) // _ROW_H) - _BUFFER_ROWS)
        visible_rows = (view_h // _ROW_H) + 2 * _BUFFER_ROWS + 2
        last_row = min(len(self._rows), first_row + visible_rows)

        if not force and first_row == self._first_drawn and last_row == self._last_drawn:
            return  # same window already drawn — nothing to do

        self._canvas.delete("rowitem")
        for i in range(first_row, last_row):
            self._draw_row(i)

        self._first_drawn = first_row
        self._last_drawn = last_row

    def _draw_row(self, i: int) -> None:
        cells, tag = self._rows[i]
        y0 = i * _ROW_H
        y1 = y0 + _ROW_H
        bg = self._alt_bg[i % 2]
        fg = self._colors.get(tag, self._colors[""])

        # Row background rectangle
        self._canvas.create_rectangle(
            0, y0, self._total_w, y1,
            fill=bg, outline="", tags="rowitem",
        )

        # Cell text items
        x = 6
        for ci, (_hdr_text, w) in enumerate(self._columns):
            cell_text = cells[ci] if ci < len(cells) else ""
            # Last column (status/tier) uses tag color; others use secondary
            col_fg = fg if ci == len(self._columns) - 1 else TOKENS["text_secondary"]
            if ci == 0:
                col_fg = TOKENS["text_primary"]   # address always primary

            self._canvas.create_text(
                x + 4, y0 + _ROW_H // 2,
                text=cell_text,
                anchor="w",
                fill=col_fg,
                font=self._font,
                tags="rowitem",
            )
            x += w

        # Row bottom border
        self._canvas.create_line(
            0, y1, self._total_w, y1,
            fill=self._border, tags="rowitem",
        )

    def _update_scroll_region(self) -> None:
        total_h = max(len(self._rows) * _ROW_H, 1)
        # Cheap — this is just two numbers, no items are touched
        self._canvas.configure(scrollregion=(0, 0, self._total_w, total_h))

    def _on_resize(self, event: tk.Event) -> None:
        self._redraw_visible(force=True)
