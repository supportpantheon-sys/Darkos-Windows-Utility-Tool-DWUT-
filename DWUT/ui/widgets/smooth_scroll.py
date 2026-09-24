"""
ui/widgets/smooth_scroll.py - drop-in CTkScrollableFrame fix.
"""
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from ui.theme import TOKENS


class SmoothScrollFrame(ctk.CTkScrollableFrame):
    """
    CTkScrollableFrame with scroll-time jank fixed.

    Two problems, two fixes:
    1. CTkScrollableFrame calls canvas.configure(scrollregion=...)
       synchronously on every <Configure> event, forcing an immediate full
       repaint — this caused a white flicker on resize. Debounced with a
       16ms after() call so multiple configure events in one frame batch
       into one repaint.
    2. That same recompute was ALSO firing mid-scroll on some systems (the
       canvas view changing can itself trigger <Configure> on the interior
       frame), which is what caused cards to visibly "smush" while actively
       scrolling — a scrollregion recompute mid-pan momentarily changes the
       canvas's own layout math while the view is already moving. Fixed by
       tracking "is the wheel actively spinning" and holding the recompute
       until scrolling has actually settled (120ms of no new wheel events),
       instead of applying the 16ms debounce indiscriminately.
    """

    def __init__(self, parent, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._scroll_update_id: str | None = None
        self._scrolling_settle_id: str | None = None
        self._is_scrolling = False

        try:
            canvas = getattr(self, "_parent_canvas", None) or getattr(self, "_canvas", None)
            if canvas:
                self._patch_canvas(canvas)
        except Exception:
            pass

        # Track active scrolling across the usual wheel/trackpad/touch event
        # names so the settle-based hold-off applies regardless of input
        # device. bind (not bind_all) so this only affects this frame's own
        # scroll activity, not scrolling elsewhere in the app.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._on_wheel_activity, add=True)

    def _on_wheel_activity(self, event=None) -> None:
        self._is_scrolling = True
        if self._scrolling_settle_id:
            self.after_cancel(self._scrolling_settle_id)
        self._scrolling_settle_id = self.after(120, self._on_scroll_settled)

    def _on_scroll_settled(self) -> None:
        self._scrolling_settle_id = None
        self._is_scrolling = False
        # A resize/content-change may have been queued while scrolling was
        # in progress — apply it now that the view has actually settled.
        if self._scroll_update_id is not None:
            self._update_region()

    def _patch_canvas(self, canvas: tk.Canvas) -> None:
        # Unbind the existing <Configure> on the scrollable child frame
        try:
            child = self._parent_frame if hasattr(self, "_parent_frame") else None
            if child:
                child.unbind("<Configure>")
                child.bind("<Configure>", self._debounced_configure, add=False)
        except Exception:
            pass

    def _debounced_configure(self, event=None) -> None:
        if self._scroll_update_id:
            self.after_cancel(self._scroll_update_id)
        if self._is_scrolling:
            # Hold the recompute — _on_scroll_settled will apply it once
            # the wheel actually stops, instead of fighting the pan.
            self._scroll_update_id = "pending"
            return
        self._scroll_update_id = self.after(16, self._update_region)

    def _update_region(self) -> None:
        self._scroll_update_id = None
        try:
            canvas = getattr(self, "_parent_canvas", None) or getattr(self, "_canvas", None)
            if canvas:
                canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

