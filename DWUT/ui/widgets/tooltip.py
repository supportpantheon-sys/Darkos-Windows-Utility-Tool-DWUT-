"""
ui/widgets/tooltip.py

Two reusable components:

  InfoBadge   — a small (i) circle that shows a tooltip popup on hover.
                Drop it anywhere next to a label.
  Tooltip     — a plain text tooltip attached to any existing widget.

Usage:
    from ui.widgets.tooltip import InfoBadge, attach_tooltip

    # Next to a label
    InfoBadge(parent, text="Disables Xbox Game Bar overlay and DVR recording.")

    # On an existing widget
    attach_tooltip(my_button, "Click to run SFC /scannow on the system files.")
"""

from __future__ import annotations

import customtkinter as ctk
import tkinter as tk

from ui.theme import TOKENS, get_font


class _TooltipPopup(tk.Toplevel):
    """The floating tooltip window."""

    def __init__(self, parent: tk.Widget, text: str) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=TOKENS["bg_card"])

        # Subtle border frame
        border = tk.Frame(self, bg=TOKENS["border"], bd=0)
        border.pack(padx=1, pady=1)

        inner = tk.Frame(border, bg=TOKENS["bg_card"])
        inner.pack()

        label = tk.Label(
            inner,
            text=text,
            wraplength=280,
            justify="left",
            bg=TOKENS["bg_card"],
            fg=TOKENS["text_primary"],
            font=get_font(10),
            padx=10,
            pady=7,
        )
        label.pack()

    def place_near(self, x: int, y: int) -> None:
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()

        # Position just below and to the right of the cursor
        sx = x + 12
        sy = y + 12

        # Clamp to screen
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        if sx + w > sw:
            sx = x - w - 4
        if sy + h > sh:
            sy = y - h - 4

        self.geometry(f"+{sx}+{sy}")


def attach_tooltip(widget: tk.Widget, text: str) -> None:
    """
    Attach a hover tooltip to any existing widget.
    Does nothing if `text` is empty.
    """
    if not text:
        return

    _popup: list[_TooltipPopup | None] = [None]

    def _on_enter(event: tk.Event) -> None:
        if _popup[0]:
            return
        tip = _TooltipPopup(widget, text)
        tip.place_near(event.x_root, event.y_root)
        _popup[0] = tip

    def _on_leave(event: tk.Event) -> None:
        if _popup[0]:
            _popup[0].destroy()
            _popup[0] = None

    def _on_motion(event: tk.Event) -> None:
        if _popup[0]:
            _popup[0].place_near(event.x_root, event.y_root)

    widget.bind("<Enter>", _on_enter)
    widget.bind("<Leave>", _on_leave)
    widget.bind("<Motion>", _on_motion)


class InfoBadge(ctk.CTkLabel):
    """
    A small circular (i) label that shows a tooltip on hover.

    Parameters
    ----------
    parent  : CTk parent widget
    text    : tooltip text shown on hover
    size    : font size of the (i) character (default 10)
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        size: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            text="i",
            width=16,
            height=16,
            corner_radius=8,
            fg_color=TOKENS["bg_input"],
            text_color=TOKENS["text_secondary"],
            font=get_font(size, "bold"),
            cursor="question_arrow",
            **kwargs,
        )

        self._tip_text = text
        self._popup: _TooltipPopup | None = None

        self.bind("<Enter>",  self._on_enter)
        self.bind("<Leave>",  self._on_leave)
        self.bind("<Motion>", self._on_motion)

    def _on_enter(self, event: tk.Event) -> None:
        if self._popup or not self._tip_text:
            return
        self._popup = _TooltipPopup(self, self._tip_text)
        self._popup.place_near(event.x_root, event.y_root)

    def _on_leave(self, event: tk.Event) -> None:
        if self._popup:
            self._popup.destroy()
            self._popup = None

    def _on_motion(self, event: tk.Event) -> None:
        if self._popup:
            self._popup.place_near(event.x_root, event.y_root)
