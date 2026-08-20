"""The single source of every colour, radius, and border width.

TOKENS is mutated in place by set_palette() and must NEVER be rebound: the modules
that paint manually hold a module-level reference to it and read it at paint time.
"""

import logging

log = logging.getLogger(__name__)

THEMES = ("light", "dark")
DEFAULT_THEME = "light"

# Theme-independent.
_GEOMETRY = {
    "radius_control": "4px",
    "radius_panel":   "6px",
    "border_width":   "1px",
}

_LIGHT = {
    # Surfaces (bg_surface is the brightest: the elevated one, sitting on bg_window)
    "bg_window":      "#F4F5F7",   # window / app chrome
    "bg_surface":     "#FFFFFF",   # content panels, table body, inputs
    "bg_subtle":      "#EDEFF2",   # table headers, inactive tabs, hover fills
    "bg_sunken":      "#E4E7EB",   # pressed fills, track grooves

    # Lines
    "border":         "#D3D8DE",   # every 1px border, default
    "border_strong":  "#B4BCC5",   # only where two bordered surfaces meet

    # Text
    "text":           "#1D2429",
    "text_muted":     "#5B6672",   # captions, help text, secondary labels
    "text_disabled":  "#9AA4AE",
    "text_on_accent": "#FFFFFF",

    # Accent (single)
    "accent":         "#2B5D8A",
    "accent_hover":   "#24507A",
    "accent_press":   "#1C4266",
    "accent_subtle":  "#DDE7F1",   # selected row background when unfocused
    "accent_focus":   "#4A85BE",   # focus border only

    # Danger (single)
    "danger":         "#A8322A",
    "danger_hover":   "#8E2822",
    "danger_subtle":  "#F6E3E1",
}

_DARK = {
    # Surfaces. The ladder runs the OPPOSITE way from light — deeper means
    # LIGHTER here, so hover (bg_subtle) is lighter than the resting fill.
    "bg_window":      "#191D23",
    "bg_surface":     "#22272E",
    "bg_subtle":      "#2C333B",
    "bg_sunken":      "#363E47",

    # Lines
    "border":         "#383F48",   # 1.41:1 on bg_surface
    "border_strong":  "#4C5560",

    # Text. Off-white, not #FFFFFF: pure white halates around glyph stems at 10pt.
    "text":           "#E6EAEF",   # 12.44:1 on bg_surface
    "text_muted":     "#A3ADB8",   # 5.61:1 on bg_subtle
    "text_disabled":  "#78828E",   # 3.27:1 on bg_subtle
    # Dark, not white: the dark accent is bright enough that white-on-accent
    # fails AA, which is also why check.svg needs a per-theme variant.
    "text_on_accent": "#0F1419",   # 7.01:1 on accent

    # Accent (single), brighter than the light accent rather than darker.
    "accent":         "#62A5DE",   # 6.41:1 on bg_window, 5.69:1 on bg_surface
    "accent_hover":   "#7CB5E6",
    "accent_press":   "#4E92CB",
    "accent_subtle":  "#1E2E3E",   # 5.25:1 for accent text on it (status pill)
    "accent_focus":   "#85BAE8",   # focus border only

    # Danger (single)
    "danger":         "#E5706A",   # 4.91:1 on bg_surface, 4.71:1 on danger_subtle
    "danger_hover":   "#F08A84",
    "danger_subtle":  "#3A2422",
}

PALETTES = {
    "light": {**_LIGHT, **_GEOMETRY},
    "dark": {**_DARK, **_GEOMETRY},
}

# The ACTIVE palette, read by theme/loader.py and at paint time by the two modules
# that paint manually. Starts light so a bare import already yields a usable palette.
TOKENS = dict(PALETTES[DEFAULT_THEME])


def resolve_theme(name) -> str:
    """Normalise a configured theme name; an unknown value logs and falls back to light."""
    candidate = (name or "").strip().lower()
    if candidate in THEMES:
        return candidate
    if candidate:
        log.warning(
            "resolve_theme: unknown theme %r — using %r. Valid: %s",
            name, DEFAULT_THEME, ", ".join(THEMES),
        )
    return DEFAULT_THEME


def set_palette(name) -> dict:
    """Make `name` active by mutating TOKENS in place; returns that same dict, not a copy."""
    theme = resolve_theme(name)
    incoming = PALETTES[theme]

    # A one-sided token addition would otherwise surface as a literal "$token_name"
    # in the rendered QSS for one theme only.
    missing = set(TOKENS) - set(incoming)
    extra = set(incoming) - set(TOKENS)
    if missing or extra:
        raise KeyError(
            f"theme palettes are out of sync for {theme!r}: "
            f"missing={sorted(missing)} extra={sorted(extra)}. "
            "Every token must exist in BOTH _LIGHT and _DARK."
        )

    TOKENS.update(incoming)
    return TOKENS


# Not theme-dependent. Lives here so theme/loader.py::apply_theme stays the only
# place QApplication.setFont is called.
FONT_FAMILY = "Segoe UI"
FONT_SIZE_PT = 10
