"""
ui/window.py — Main application window.

Layout uses grid (not pack) for the sidebar + content split so that
column weights respond correctly to window resize events.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.config import config
from app.state import state
from core.events import Events, bus
from core.logger import get_logger
from ui.geometry import MIN_W, MIN_H, resolve_geometry, get_physical_screen_size
from ui.win_effects import round_window_corners
from ui.theme import TOKENS, get_font

_log = get_logger(__name__)


class MainWindow:
    def __init__(self) -> None:
        self.root = ctk.CTk()
        self.root.title("Darko's Windows Utility Tool")

        # overrideredirect MUST be set before geometry is applied — doing it
        # after (as this briefly did) can make Windows shift the window's
        # actual displayed position when the decoration changes, which is
        # exactly why the splash (which already set this before geometry)
        # and the main window were landing in different spots even though
        # both computed the identical (x, y) from resolve_geometry().
        self.root.overrideredirect(True)
        round_window_corners(self.root)
        self.root.bind("<Map>", self._on_map)
        self._resizing = False

        self.root.minsize(MIN_W, MIN_H)
        self.root.configure(fg_color=TOKENS["bg_shell"])

        # Same pure (screen size) -> (x, y, w, h) calculation the splash
        # screen just used, so the main window opens at EXACTLY the size
        # and position the splash was already showing — set directly, no
        # animation or transient in-between state that could leave it
        # looking off-center if anything interrupted it.
        self.root.update_idletasks()
        screen_w, screen_h = get_physical_screen_size()
        if screen_w <= 0 or screen_h <= 0:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
        x, y, w, h = resolve_geometry(screen_w, screen_h)
        # wm_geometry (not geometry!) — customtkinter's CTk class overrides
        # .geometry() to silently multiply width/height by a detected
        # per-monitor DPI scaling factor (confirmed by reading customtkinter's
        # own source: ctk_tk.py's geometry() calls _apply_geometry_scaling()).
        # Plain tk.Tk() (what the splash uses) has no such override, so the
        # exact same (w, h) request was landing at two DIFFERENT actual sizes
        # depending on which window class received it — which is enough to
        # make two windows that both "center" the same numbers look like
        # they're in different places. wm_geometry is inherited straight
        # from tkinter's Wm mixin and is never touched by customtkinter, so
        # it applies the numbers completely unscaled — identical to what
        # the splash's plain Tk root does.
        self.root.wm_geometry(f"{w}x{h}+{x}+{y}")
        self.root.after(30, lambda: self.root.wm_geometry(f"{w}x{h}+{x}+{y}"))
        self.root.after(150, lambda: self.root.wm_geometry(f"{w}x{h}+{x}+{y}"))
        self.root.after(400, lambda: self.root.wm_geometry(f"{w}x{h}+{x}+{y}"))
        _log.info(
            "Window geometry: screen=%sx%s -> %sx%s+%s+%s",
            screen_w, screen_h, w, h, x, y,
        )

        # Grid: row-0 = custom titlebar (fixed), row-1 = sidebar + content
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=0)
        self.root.rowconfigure(1, weight=1)

        self._pages: dict[str, object] = {}
        self._current_page: str = ""

        self._build_layout()
        self._subscribe_events()

        # Navigate to start page after a short delay so the window is visible first
        self.root.after(50, lambda: self.navigate(config.start_page))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Custom titlebar (drag / minimize / close) ───────────────────────────

    def _build_titlebar(self) -> None:
        bar = ctk.CTkFrame(self.root, fg_color=TOKENS["sidebar_bg"], corner_radius=0, height=30)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        self._titlebar = bar

        # Anything in the bar EXCEPT the buttons drags the window
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>", self._drag_move)
        bar.bind("<Double-Button-1>", lambda e: self._toggle_maximize())

        btns = ctk.CTkFrame(bar, fg_color="transparent")
        btns.pack(side="right", fill="y")

        close_btn = ctk.CTkButton(
            btns, text="×", width=42, height=30, corner_radius=0,
            fg_color="transparent", hover_color=TOKENS["error"],
            text_color=TOKENS["text_secondary"], font=get_font(14),
            command=self._on_close,
        )
        close_btn.pack(side="right")

        min_btn = ctk.CTkButton(
            btns, text="—", width=42, height=30, corner_radius=0,
            fg_color="transparent", hover_color=TOKENS["bg_hover"],
            text_color=TOKENS["text_secondary"], font=get_font(11),
            command=self._minimize,
        )
        min_btn.pack(side="right")

    def _drag_start(self, event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not hasattr(self, "_drag_offset"):
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.wm_geometry(f"+{x}+{y}")

    def _toggle_maximize(self) -> None:
        # overrideredirect windows don't get native maximize — approximate
        # it by filling the current monitor's work area.
        if getattr(self, "_maximized", False):
            x, y, w, h = self._restore_geom
            self.root.wm_geometry(f"{w}x{h}+{x}+{y}")
            self._maximized = False
        else:
            self._restore_geom = (
                self.root.winfo_x(), self.root.winfo_y(),
                self.root.winfo_width(), self.root.winfo_height(),
            )
            sw, sh = get_physical_screen_size()
            if sw <= 0 or sh <= 0:
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
            self.root.wm_geometry(f"{sw}x{sh}+0+0")
            self._maximized = True

    def _minimize(self) -> None:
        # Known working pattern for minimizing an overrideredirect() window
        # on Windows: drop back to a normal window right before iconify()
        # (iconify on a fully undecorated window is unreliable — it can
        # fail to show in the taskbar or fail to restore), then re-apply
        # overrideredirect once _on_map sees it come back.
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, event) -> None:
        # Fires on restore-from-taskbar (and on initial show). Only strip
        # the titlebar back off once the window is actually in its normal
        # (not minimized) state, or this fights the iconify animation.
        if self.root.state() == "normal" and not self.root.overrideredirect():
            self.root.overrideredirect(True)
            # Toggling overrideredirect recreates the underlying HWND on
            # Windows, which resets the rounded-corner DWM attribute — has
            # to be re-applied every time this happens, not just once at
            # startup.
            round_window_corners(self.root)

    def _build_resize_grip(self) -> None:
        """Small bottom-right corner handle to restore manual resizing,
        which overrideredirect() removes entirely."""
        grip = tk.Frame(self.root, bg=TOKENS["border"], cursor="bottom_right_corner",
                         width=14, height=14)
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    def _resize_start(self, event) -> None:
        self._resizing = True
        self._resize_origin = (
            event.x_root, event.y_root,
            self.root.winfo_width(), self.root.winfo_height(),
        )

    def _resize_move(self, event) -> None:
        if not self._resizing or not hasattr(self, "_resize_origin"):
            return
        ox, oy, ow, oh = self._resize_origin
        w = max(ow + (event.x_root - ox), MIN_W)
        h = max(oh + (event.y_root - oy), MIN_H)
        self.root.wm_geometry(f"{w}x{h}")

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_layout(self) -> None:
        from ui.sidebar import Sidebar

        self._build_titlebar()

        self._sidebar = Sidebar(self.root, on_navigate=self.navigate)
        self._sidebar.frame.grid(row=1, column=0, sticky="ns")

        self._content = ctk.CTkFrame(
            self.root, fg_color=TOKENS["bg_base"], corner_radius=0,
        )
        self._content.grid(row=1, column=1, sticky="nsew")
        self._content.rowconfigure(0, weight=1)
        self._content.columnconfigure(0, weight=1)

        self._build_resize_grip()
        self._build_loading_overlay()

        # Pages are registered here (cheap — just names) but NOT built yet.
        # Each page is only imported + instantiated the first time the user
        # actually navigates to it — see _ensure_page_built(). Building all
        # 7 pages up front (Tools/Proxy especially — hundreds of widgets
        # each) was the single biggest contributor to startup lag.
        self._page_specs: dict[str, tuple[str, str]] = {
            "dashboard": ("ui.pages.dashboard", "DashboardPage"),
            "optimizer": ("ui.pages.optimizer", "OptimizerPage"),
            "gaming":    ("ui.pages.gaming",    "GamingPage"),
            "debloat":   ("ui.pages.debloat",   "DebloatPage"),
            "proxy":     ("ui.pages.proxy",     "ProxyPage"),
            "tools":     ("ui.pages.tools",     "ToolsPage"),
            "settings":  ("ui.pages.settings",  "SettingsPage"),
        }

    def _build_loading_overlay(self) -> None:
        """
        A brief loading screen shown only while a page is being built for
        the first time (already-visited pages just raise/lower instantly,
        no overlay). Its duration isn't a fixed timer — it's shown, forced
        to paint via update_idletasks(), and only THEN does the actual page
        construction run synchronously; the overlay naturally stays up for
        exactly as long as that construction takes and disappears the
        instant it's done, so it scales itself to however slow or fast a
        given page's first build actually is instead of guessing.
        """
        self._loading_overlay = ctk.CTkFrame(
            self._content, fg_color=TOKENS["bg_base"], corner_radius=0,
        )
        ctk.CTkLabel(
            self._loading_overlay, text="Loading…",
            font=get_font(13), text_color=TOKENS["text_secondary"],
        ).place(relx=0.5, rely=0.5, anchor="center")
        # Not placed yet — shown on demand in navigate()

    def _ensure_page_built(self, name: str) -> bool:
        """Lazily import + construct a page on first visit. Returns False
        (and logs) if the page failed to build, same as the old eager
        registration did on import/construction errors."""
        if name in self._pages:
            return True

        spec = self._page_specs.get(name)
        if not spec:
            _log.warning("Unknown page: %s", name)
            return False

        mod_path, class_name = spec
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, class_name)
            page = cls(self._content)
            # Use place so pages stack precisely inside _content
            page.frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            page.frame.lower()
            self._pages[name] = page
            return True
        except Exception as exc:
            _log.error("Failed to load page '%s': %s", name, exc, exc_info=True)
            return False

    # ── Navigation ────────────────────────────────────────────────────────

    def navigate(self, page_name: str) -> None:
        if page_name not in self._page_specs:
            _log.warning("Unknown page: %s", page_name)
            return

        first_build = page_name not in self._pages
        if first_build:
            # Show + force-paint the overlay BEFORE building — Tk can't
            # repaint while the main thread is busy building the page, so
            # this has to happen first or the overlay would never actually
            # become visible during a synchronous build.
            self._loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._loading_overlay.lift()
            self._loading_overlay.update_idletasks()

        if not self._ensure_page_built(page_name):
            if first_build:
                self._loading_overlay.place_forget()
            return

        if self._current_page and self._current_page in self._pages:
            self._pages[self._current_page].frame.lower()
            if hasattr(self._pages[self._current_page], "on_hide"):
                self._pages[self._current_page].on_hide()

        self._current_page = page_name
        state.current_page = page_name
        self._pages[page_name].frame.lift()
        if hasattr(self._pages[page_name], "on_show"):
            self._pages[page_name].on_show()

        if first_build:
            self._loading_overlay.place_forget()

        self._sidebar.set_active(page_name)
        _log.debug("Navigated to '%s'", page_name)

    # ── Events ────────────────────────────────────────────────────────────

    def _subscribe_events(self) -> None:
        bus.subscribe(Events.PAGE_CHANGE,  self._on_page_change)
        bus.subscribe(Events.THEME_CHANGE, self._on_theme_change)

    def _on_page_change(self, page_name: str) -> None:
        if page_name != self._current_page:
            self.navigate(page_name)

    def _on_theme_change(self, theme_name: str) -> None:
        from ui.theme import apply_theme
        apply_theme(theme_name)
        config.theme = theme_name
        config.save()
        self.reload_theme()

    def reload_theme(self) -> None:
        """
        Rebuild the entire sidebar + page tree against the freshly-applied
        TOKENS so a theme switch takes effect immediately — no restart.
        """
        current = self._current_page or config.start_page
        for widget in self.root.winfo_children():
            widget.destroy()
        self._pages.clear()
        self._current_page = ""
        self.root.configure(fg_color=TOKENS["bg_shell"])
        self._build_layout()
        self.navigate(current)
        from ui.theme import current_theme
        _log.info("Theme reloaded live: %s", current_theme())

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        state.shutting_down = True
        try:
            config.window_width  = self.root.winfo_width()
            config.window_height = self.root.winfo_height()
            config.start_page    = self._current_page
            config.save()
        except Exception:
            pass

        for mod_attr, cls_attr in [
            ("modules.gaming.fps_unlock", "fps_unlocker"),
            ("modules.gaming.booster",    "booster"),
        ]:
            try:
                import importlib
                m = importlib.import_module(mod_attr)
                obj = getattr(m, cls_attr)
                if getattr(obj, "is_active", False):
                    obj.disable() if hasattr(obj, "disable") else obj.deactivate()
            except Exception:
                pass

        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
