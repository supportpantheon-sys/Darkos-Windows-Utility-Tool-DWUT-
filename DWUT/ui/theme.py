"""
ui/theme.py — Complete theme system for DWUT.

14 named themes, all sharing the same token keys.
Swapping a theme = swapping values, zero widget-code changes.

Unlike the old set (12 near-identical dark grays with a recolored accent),
every theme here overrides its full surface palette — background, sidebar,
borders and semantic colors — not just the accent, so switching themes is
an actually visible change. Two light themes are included for real range.
Theme changes also apply live (the whole UI tree rebuilds on switch) —
see MainWindow.reload_theme() in ui/window.py — no restart needed.

Themes:
  dark, midnight, obsidian, abyss,
  violet, crimson, ember, emerald,
  nord, dracula, solarized, high_contrast,
  daylight, paper
"""

from __future__ import annotations
from typing import Literal, Any

# ── All legal theme names ─────────────────────────────────────────────────────

ThemeName = Literal[
    "dark", "midnight", "obsidian", "abyss",
    "violet", "crimson", "ember", "emerald",
    "nord", "dracula", "solarized", "high_contrast",
    "daylight", "paper",
]

THEME_DISPLAY_NAMES: dict[str, str] = {
    "dark":          "Dark",
    "midnight":      "Midnight",
    "obsidian":      "Obsidian",
    "abyss":         "Abyss",
    "violet":        "Violet",
    "crimson":       "Crimson",
    "ember":         "Ember",
    "emerald":       "Emerald",
    "nord":          "Nord",
    "dracula":       "Dracula",
    "solarized":     "Solarized Dark",
    "high_contrast": "High Contrast",
    "daylight":      "Daylight",
    "paper":         "Paper",
}

# Themes with a light background — widgets that need to pick between
# light/dark chrome (e.g. window titlebar icons) can check this.
LIGHT_THEMES: frozenset[str] = frozenset({"daylight", "paper"})

# ── Base token set (Dark theme) ───────────────────────────────────────────────

_BASE: dict[str, str] = {
    "bg_shell":             "#0D0F12",
    "bg_base":              "#13161A",
    "bg_card":              "#1A1E24",
    "bg_input":             "#20252D",
    "bg_hover":             "#252B35",

    "border":               "#2A3040",
    "border_focus":         "#4A7CFF",

    "accent":               "#4A7CFF",
    "accent_dim":           "#2A4A99",
    "accent_text":          "#FFFFFF",
    "accent_subtle":        "#1A2A55",

    "success":              "#34C759",
    "success_bg":           "#0D2B18",
    "warning":              "#FF9F0A",
    "warning_bg":           "#2B1F0A",
    "error":                "#FF453A",
    "error_bg":             "#2B0F0D",
    "info":                 "#5AC8FA",
    "info_bg":              "#0D1E2B",

    "text_primary":         "#E8ECF0",
    "text_secondary":       "#8A95A3",
    "text_disabled":        "#4A5260",
    "text_on_accent":       "#FFFFFF",

    "sidebar_bg":           "#0A0C0F",
    "sidebar_active":       "#1A1E24",
    "sidebar_active_border":"#4A7CFF",
    "sidebar_icon_active":  "#4A7CFF",
    "sidebar_icon_rest":    "#5A6475",

    "scrollbar":            "#2A3040",
    "scrollbar_hover":      "#3B4560",

    "tag_high":             "#FF453A",
    "tag_medium":           "#FF9F0A",
    "tag_low":              "#34C759",
    "tag_text":             "#FFFFFF",
}

# ── All 14 themes ─────────────────────────────────────────────────────────────
#
# Every theme below overrides the full surface stack (bg_shell → bg_hover,
# sidebar, borders) as well as the accent, so each one reads as a genuinely
# different app skin rather than a recolored dot. Semantic colors (success/
# warning/error/info) are tuned per-theme too so they stay readable on that
# theme's background instead of reusing the dark-theme defaults everywhere.

_THEMES: dict[str, dict[str, str]] = {

    # 1. Dark — the base, no overrides
    "dark": {},

    # 2. Midnight — true OLED black, icy blue accent
    "midnight": {
        "bg_shell":             "#000000",
        "bg_base":              "#050608",
        "bg_card":              "#0A0C10",
        "bg_input":             "#101218",
        "bg_hover":             "#161A22",
        "border":               "#1C2230",
        "border_focus":         "#5CC8FF",
        "accent":               "#5CC8FF",
        "accent_dim":           "#2E8FC7",
        "accent_subtle":        "#0A2230",
        "sidebar_bg":           "#000000",
        "sidebar_active":       "#0A0C10",
        "sidebar_icon_active":  "#5CC8FF",
        "sidebar_active_border":"#5CC8FF",
        "info":                 "#5CC8FF",
        "info_bg":              "#08222E",
    },

    # 3. Obsidian — warm graphite/charcoal, amber accent (was "Carbon")
    "obsidian": {
        "bg_shell":             "#15130F",
        "bg_base":              "#1C1915",
        "bg_card":              "#26221C",
        "bg_input":             "#302B23",
        "bg_hover":             "#3A342A",
        "border":               "#42392C",
        "border_focus":         "#E0A458",
        "accent":               "#E0A458",
        "accent_dim":           "#A97530",
        "accent_text":          "#1A1408",
        "accent_subtle":        "#3A2A12",
        "sidebar_bg":           "#100E0B",
        "sidebar_active":       "#26221C",
        "sidebar_icon_active":  "#E0A458",
        "sidebar_active_border":"#E0A458",
        "text_primary":         "#F0EAE0",
        "text_secondary":       "#A69C8C",
        "text_disabled":        "#5C5548",
        "scrollbar":            "#42392C",
        "scrollbar_hover":      "#584C3A",
    },

    # 4. Abyss — deep navy ocean, cyan accent (was "Slate")
    "abyss": {
        "bg_shell":             "#050C16",
        "bg_base":              "#08111E",
        "bg_card":              "#0D1826",
        "bg_input":             "#122032",
        "bg_hover":             "#17283E",
        "border":               "#1D3552",
        "border_focus":         "#22D3EE",
        "accent":               "#22D3EE",
        "accent_dim":           "#0E97AC",
        "accent_text":          "#03161A",
        "accent_subtle":        "#0A2A32",
        "sidebar_bg":           "#03090F",
        "sidebar_active":       "#0D1826",
        "sidebar_icon_active":  "#22D3EE",
        "sidebar_active_border":"#22D3EE",
        "text_primary":         "#DCEEF5",
        "text_secondary":       "#7C97AC",
        "info":                 "#22D3EE",
        "info_bg":              "#0A2A32",
    },

    # 5. Violet — deep plum background, purple accent
    "violet": {
        "bg_shell":             "#0E0A18",
        "bg_base":              "#150F22",
        "bg_card":              "#1D152E",
        "bg_input":             "#251B3A",
        "bg_hover":             "#2D2246",
        "border":               "#372A50",
        "border_focus":         "#A78BFA",
        "accent":               "#A78BFA",
        "accent_dim":           "#7C3AED",
        "accent_text":          "#0E0A18",
        "accent_subtle":        "#241A42",
        "sidebar_bg":           "#0A0714",
        "sidebar_active":       "#1D152E",
        "sidebar_icon_active":  "#A78BFA",
        "sidebar_active_border":"#A78BFA",
        "text_primary":         "#EDE7FA",
        "text_secondary":       "#9385AE",
        "tag_high":             "#F43F5E",
        "tag_medium":           "#F59E0B",
        "tag_low":              "#34D399",
    },

    # 6. Crimson — deep maroon background, rose accent (was "Rose")
    "crimson": {
        "bg_shell":             "#140507",
        "bg_base":              "#1C0B0D",
        "bg_card":              "#251114",
        "bg_input":             "#30171A",
        "bg_hover":             "#3A1D21",
        "border":               "#452227",
        "border_focus":         "#FB7185",
        "accent":               "#FB7185",
        "accent_dim":           "#BE1239",
        "accent_text":          "#160406",
        "accent_subtle":        "#341419",
        "sidebar_bg":           "#0F0304",
        "sidebar_active":       "#251114",
        "sidebar_icon_active":  "#FB7185",
        "sidebar_active_border":"#FB7185",
        "text_primary":         "#F7E6E8",
        "text_secondary":       "#B08A8E",
    },

    # 7. Ember — warm dark brown/orange, glowing amber accent (was "Amber")
    "ember": {
        "bg_shell":             "#150C06",
        "bg_base":              "#1E120A",
        "bg_card":              "#28180E",
        "bg_input":             "#331E12",
        "bg_hover":             "#3E2517",
        "border":               "#4A2C1A",
        "border_focus":         "#FB923C",
        "accent":               "#FB923C",
        "accent_dim":           "#C2540D",
        "accent_text":          "#180D04",
        "accent_subtle":        "#3D2308",
        "sidebar_bg":           "#100804",
        "sidebar_active":       "#28180E",
        "sidebar_icon_active":  "#FB923C",
        "sidebar_active_border":"#FB923C",
        "text_primary":         "#F7E9DC",
        "text_secondary":       "#B69A82",
        "warning":              "#FBBF24",
        "warning_bg":           "#3D2A08",
    },

    # 8. Emerald — deep forest green base, minty accent (was "Teal")
    "emerald": {
        "bg_shell":             "#040F0B",
        "bg_base":              "#08160F",
        "bg_card":              "#0D1F16",
        "bg_input":             "#12291C",
        "bg_hover":             "#173322",
        "border":               "#1D3D28",
        "border_focus":         "#34D399",
        "accent":               "#34D399",
        "accent_dim":           "#0D9668",
        "accent_text":          "#03130C",
        "accent_subtle":        "#0F2E1E",
        "sidebar_bg":           "#020B07",
        "sidebar_active":       "#0D1F16",
        "sidebar_icon_active":  "#34D399",
        "sidebar_active_border":"#34D399",
        "text_primary":         "#E1F5EA",
        "text_secondary":       "#82AC96",
        "success":              "#34D399",
        "success_bg":           "#0F2E1E",
    },

    # 9. Nord — Nordic palette
    "nord": {
        "bg_shell":             "#1C2029",
        "bg_base":              "#232A35",
        "bg_card":              "#2A3242",
        "bg_input":             "#303B4E",
        "bg_hover":             "#374355",
        "sidebar_bg":           "#181F28",
        "sidebar_active":       "#2A3242",
        "border":               "#3B4E65",
        "border_focus":         "#81A1C1",
        "accent":               "#81A1C1",
        "accent_dim":           "#5E81AC",
        "accent_subtle":        "#1F2D3D",
        "border_focus":         "#81A1C1",
        "sidebar_icon_active":  "#88C0D0",
        "sidebar_active_border":"#81A1C1",
        "text_primary":         "#ECEFF4",
        "text_secondary":       "#9AA5B5",
        "success":              "#A3BE8C",
        "warning":              "#EBCB8B",
        "error":                "#BF616A",
        "info":                 "#88C0D0",
    },

    # 10. Dracula — classic dark theme
    "dracula": {
        "bg_shell":             "#1A1B26",
        "bg_base":              "#21222C",
        "bg_card":              "#282A36",
        "bg_input":             "#313342",
        "bg_hover":             "#383B4E",
        "sidebar_bg":           "#171820",
        "sidebar_active":       "#282A36",
        "border":               "#44475A",
        "border_focus":         "#BD93F9",
        "accent":               "#BD93F9",
        "accent_dim":           "#8B5CF6",
        "accent_subtle":        "#1D1428",
        "sidebar_icon_active":  "#BD93F9",
        "sidebar_active_border":"#BD93F9",
        "text_primary":         "#F8F8F2",
        "text_secondary":       "#6272A4",
        "success":              "#50FA7B",
        "warning":              "#FFB86C",
        "error":                "#FF5555",
        "info":                 "#8BE9FD",
        "tag_high":             "#FF5555",
        "tag_medium":           "#FFB86C",
        "tag_low":              "#50FA7B",
    },

    # 11. Solarized Dark
    "solarized": {
        "bg_shell":             "#001D26",
        "bg_base":              "#002B36",
        "bg_card":              "#073642",
        "bg_input":             "#0D4555",
        "bg_hover":             "#124F60",
        "sidebar_bg":           "#001B22",
        "sidebar_active":       "#073642",
        "border":               "#1A5060",
        "border_focus":         "#268BD2",
        "accent":               "#268BD2",
        "accent_dim":           "#1A6099",
        "accent_subtle":        "#052030",
        "sidebar_icon_active":  "#2AA198",
        "sidebar_active_border":"#268BD2",
        "text_primary":         "#EEE8D5",
        "text_secondary":       "#839496",
        "text_disabled":        "#586E75",
        "success":              "#859900",
        "warning":              "#CB4B16",
        "error":                "#DC322F",
        "info":                 "#2AA198",
    },

    # 12. High Contrast — max readability
    "high_contrast": {
        "bg_shell":             "#000000",
        "bg_base":              "#0A0A0A",
        "bg_card":              "#111111",
        "bg_input":             "#181818",
        "bg_hover":             "#222222",
        "sidebar_bg":           "#000000",
        "sidebar_active":       "#111111",
        "border":               "#444444",
        "border_focus":         "#FFFFFF",
        "accent":               "#00BFFF",
        "accent_dim":           "#007AA8",
        "accent_subtle":        "#001A25",
        "sidebar_icon_active":  "#00BFFF",
        "sidebar_active_border":"#FFFFFF",
        "text_primary":         "#FFFFFF",
        "text_secondary":       "#CCCCCC",
        "text_disabled":        "#666666",
        "success":              "#00FF80",
        "warning":              "#FFCC00",
        "error":                "#FF3333",
        "info":                 "#00BFFF",
    },

    # 13. Daylight — clean light admin panel, blue accent
    "daylight": {
        "bg_shell":             "#EEF1F5",
        "bg_base":              "#F5F7FA",
        "bg_card":              "#FFFFFF",
        "bg_input":             "#F0F2F6",
        "bg_hover":             "#E6E9EF",
        "border":               "#DCE0E8",
        "border_focus":         "#2563EB",
        "accent":               "#2563EB",
        "accent_dim":           "#1D4ED8",
        "accent_text":          "#FFFFFF",
        "accent_subtle":        "#DBE6FE",
        "success":              "#16A34A",
        "success_bg":           "#E8F8ED",
        "warning":              "#D97706",
        "warning_bg":           "#FDF1DD",
        "error":                "#DC2626",
        "error_bg":             "#FBE7E7",
        "info":                 "#0891B2",
        "info_bg":              "#E1F5F9",
        "text_primary":         "#161B22",
        "text_secondary":       "#5B6472",
        "text_disabled":        "#A2A9B3",
        "text_on_accent":       "#FFFFFF",
        "sidebar_bg":           "#FFFFFF",
        "sidebar_active":       "#EAF0FE",
        "sidebar_active_border":"#2563EB",
        "sidebar_icon_active":  "#2563EB",
        "sidebar_icon_rest":    "#8A93A3",
        "scrollbar":            "#DCE0E8",
        "scrollbar_hover":      "#C3C9D4",
        "tag_high":             "#DC2626",
        "tag_medium":           "#D97706",
        "tag_low":              "#16A34A",
        "tag_text":             "#FFFFFF",
    },

    # 14. Paper — warm cream light theme, terracotta accent
    "paper": {
        "bg_shell":             "#F1EAE0",
        "bg_base":              "#F7F1E8",
        "bg_card":              "#FDF9F3",
        "bg_input":             "#F1E9DB",
        "bg_hover":             "#E9DFCC",
        "border":               "#E0D4BE",
        "border_focus":         "#C2622F",
        "accent":               "#C2622F",
        "accent_dim":           "#9C4C22",
        "accent_text":          "#FFFFFF",
        "accent_subtle":        "#F3DFCC",
        "success":              "#4C7A3D",
        "success_bg":           "#E8F0DF",
        "warning":              "#B4791A",
        "warning_bg":           "#F5E7CC",
        "error":                "#B23A2E",
        "error_bg":             "#F5DEDA",
        "info":                 "#2E6E7A",
        "info_bg":              "#DDEBED",
        "text_primary":         "#2B2117",
        "text_secondary":       "#6E6152",
        "text_disabled":        "#AFA48F",
        "text_on_accent":       "#FFFFFF",
        "sidebar_bg":           "#FDF9F3",
        "sidebar_active":       "#F3DFCC",
        "sidebar_active_border":"#C2622F",
        "sidebar_icon_active":  "#C2622F",
        "sidebar_icon_rest":    "#9C8C74",
        "scrollbar":            "#E0D4BE",
        "scrollbar_hover":      "#CBBB9C",
        "tag_high":             "#B23A2E",
        "tag_medium":           "#B4791A",
        "tag_low":              "#4C7A3D",
        "tag_text":             "#FFFFFF",
    },
}

# ── Live token dict — mutated by apply_theme() ────────────────────────────────

TOKENS: dict[str, str] = dict(_BASE)
_current_theme: str = "dark"


def apply_theme(name: str) -> None:
    """Mutate TOKENS in-place with overrides for `name`."""
    global _current_theme
    TOKENS.clear()
    TOKENS.update(_BASE)
    overrides = _THEMES.get(name, {})
    TOKENS.update(overrides)
    _current_theme = name

    try:
        import customtkinter as ctk
        # Every color in this app is set explicitly from TOKENS, but CTk still
        # uses appearance mode for a handful of native bits (e.g. default
        # scrollbar chrome), so keep it in sync with light vs dark themes.
        ctk.set_appearance_mode("light" if name in LIGHT_THEMES else "dark")
        ctk.set_default_color_theme("dark-blue")
    except ImportError:
        pass


def current_theme() -> str:
    return _current_theme


def all_theme_names() -> list[str]:
    return list(_THEMES.keys())


def display_name(key: str) -> str:
    return THEME_DISPLAY_NAMES.get(key, key.capitalize())


# ── Typography ────────────────────────────────────────────────────────────────

_FONT_FAMILY: str = "Segoe UI Variable"
_MONO_FAMILY: str = "Consolas"


def get_font(
    size: int = 13,
    weight: Literal["normal", "bold"] = "normal",
    mono: bool = False,
) -> tuple[str, int, str]:
    family = _MONO_FAMILY if mono else _FONT_FAMILY
    return (family, size, weight)


# ── Widget style presets ──────────────────────────────────────────────────────

def card_style() -> dict[str, Any]:
    return {
        "fg_color":     TOKENS["bg_card"],
        "corner_radius": 8,
        "border_width":  1,
        "border_color":  TOKENS["border"],
    }


def input_style() -> dict[str, Any]:
    return {
        "fg_color":     TOKENS["bg_input"],
        "border_color": TOKENS["border"],
        "text_color":   TOKENS["text_primary"],
        "corner_radius": 6,
    }


def button_style(variant: Literal["primary", "ghost", "danger", "success"] = "primary") -> dict[str, Any]:
    if variant == "primary":
        return {
            "fg_color":    TOKENS["accent"],
            "hover_color": TOKENS["accent_dim"],
            "text_color":  TOKENS["accent_text"],
            "corner_radius": 6,
            "font": get_font(12),
        }
    if variant == "ghost":
        return {
            "fg_color":    "transparent",
            "hover_color": TOKENS["bg_hover"],
            "text_color":  TOKENS["text_primary"],
            "border_width": 1,
            "border_color": TOKENS["border"],
            "corner_radius": 6,
            "font": get_font(12),
        }
    if variant == "danger":
        return {
            "fg_color":    TOKENS["error"],
            "hover_color": "#CC2E25",
            "text_color":  "#FFFFFF",
            "corner_radius": 6,
            "font": get_font(12),
        }
    if variant == "success":
        return {
            "fg_color":    TOKENS["success"],
            "hover_color": "#25A048",
            "text_color":  "#000000",
            "corner_radius": 6,
            "font": get_font(12),
        }
    return {}
