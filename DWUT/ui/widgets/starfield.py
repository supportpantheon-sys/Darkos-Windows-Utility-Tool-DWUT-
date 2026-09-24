"""
ui/widgets/starfield.py

A lightweight animated starfield background — small stars that drift
slowly and twinkle/flicker, drawn on a plain tk.Canvas.

Deliberately plain tkinter (not customtkinter) so it can be dropped into
ANY canvas-capable parent — a customtkinter frame in the main app, or a
bare tk.Tk() splash screen that never touches customtkinter at all.

Performance notes (this widget is meant to run continuously, so it has to
be cheap):
  - Star count scales with canvas area, capped, so a huge window doesn't
    spawn thousands of stars.
  - Movement uses canvas.move() (a coordinate translation) instead of
    deleting/recreating items every frame.
  - Ticks at ~20fps (50ms) — smooth enough for a background element,
    much cheaper than 60fps.
  - stop() cancels the scheduled callback; safe to call from __del__-style
    cleanup or when a screen is being torn down.

Usage:
    field = Starfield(parent, bg="#05060A", star_color="#8FA6C8")
    field.pack(fill="both", expand=True)   # or .place(...)
    field.start()
    ...
    field.stop()
"""

from __future__ import annotations

import random
import math
import tkinter as tk
from typing import Optional


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _blend(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> str:
    return _rgb_to_hex(
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


class Starfield(tk.Canvas):
    def __init__(
        self,
        parent,
        bg: str = "#05060A",
        star_color: str = "#9FB4D9",
        max_stars: int = 140,
        tick_ms: int = 50,
        **kwargs,
    ) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        super().__init__(parent, bg=bg, **kwargs)
        self._bg_rgb = _hex_to_rgb(bg)
        self._dim_rgb = self._blend_toward_bg(_hex_to_rgb(star_color), 0.55)
        self._bright_rgb = _hex_to_rgb(star_color)
        self._max_stars = max_stars
        self._tick_ms = tick_ms
        self._stars: list[dict] = []
        self._running = False
        self._after_id: Optional[str] = None
        self._seeded_for: tuple[int, int] = (0, 0)

        self.bind("<Configure>", self._on_configure)

    def _blend_toward_bg(self, rgb: tuple[int, int, int], t: float) -> tuple[int, int, int]:
        r, g, b = self._bg_rgb
        return (
            int(rgb[0] + (r - rgb[0]) * t),
            int(rgb[1] + (g - rgb[1]) * t),
            int(rgb[2] + (b - rgb[2]) * t),
        )

    # ── Setup ────────────────────────────────────────────────────────────

    def _on_configure(self, event: tk.Event) -> None:
        w, h = max(event.width, 1), max(event.height, 1)
        # Only reseed on a meaningfully different size (avoid churn on
        # every tiny resize event during a window drag)
        if abs(w - self._seeded_for[0]) < 40 and abs(h - self._seeded_for[1]) < 40 and self._stars:
            return
        self._seed(w, h)

    def _seed(self, w: int, h: int) -> None:
        self.delete("star")
        self._stars.clear()
        self._seeded_for = (w, h)

        area = w * h
        count = min(self._max_stars, max(24, area // 6000))

        for _ in range(count):
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            r = random.choice([0.6, 0.8, 1.0, 1.0, 1.3, 1.6])
            speed = random.uniform(2.0, 10.0) / 60.0   # px per tick-ish, slow drift
            vx = speed * random.uniform(-0.3, 0.3)
            vy = speed * (0.3 + random.uniform(0, 0.7))  # gentle downward-ish drift
            phase = random.uniform(0, 6.283)
            twinkle_speed = random.uniform(0.03, 0.10)
            item = self.create_oval(
                x - r, y - r, x + r, y + r,
                fill=_rgb_to_hex(*self._bright_rgb), outline="",
                tags="star",
            )
            self._stars.append({
                "id": item, "x": x, "y": y, "r": r,
                "vx": vx, "vy": vy, "phase": phase, "tspeed": twinkle_speed,
            })

    # ── Animation ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            if not self.winfo_exists():
                self._running = False
                return
        except Exception:
            self._running = False
            return

        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)

        for star in self._stars:
            star["x"] += star["vx"]
            star["y"] += star["vy"]

            # Wrap around edges so stars drift forever
            wrapped = False
            if star["x"] < -2:
                star["x"] = w + 2
                wrapped = True
            elif star["x"] > w + 2:
                star["x"] = -2
                wrapped = True
            if star["y"] < -2:
                star["y"] = h + 2
                wrapped = True
            elif star["y"] > h + 2:
                star["y"] = -2
                wrapped = True

            if wrapped:
                r = star["r"]
                self.coords(star["id"], star["x"] - r, star["y"] - r, star["x"] + r, star["y"] + r)
            else:
                self.move(star["id"], star["vx"], star["vy"])

            # Twinkle: smooth brightness oscillation via sine, plus an
            # occasional sharper "flicker" step for a handful of stars.
            star["phase"] += star["tspeed"]
            t = (math.sin(star["phase"]) + 1) / 2  # 0..1
            if random.random() < 0.012:
                t = random.choice([0.0, 1.0])   # occasional flicker snap
            color = _blend(self._dim_rgb, self._bright_rgb, t)
            self.itemconfigure(star["id"], fill=color)

        self._after_id = self.after(self._tick_ms, self._tick)
