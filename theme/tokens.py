"""
theme/tokens.py — the single source of every color, radius, and border width.

Nothing else in AutoDoc may define one of these values. Python code that
paints manually (QStyledItemDelegate subclasses, notably
sessions_window/status_delegate.py) reads from TOKENS rather than hard-coding
a hex string — that is the ONLY sanctioned reason for Python to look at a
color at all.

Rules that are easy to break by accident:
  * One accent, one danger color. The accent appears only on the primary
    button, the selected table row, the active tab, and focus borders. If it
    shows up anywhere else, that's a defect.
  * Need a lighter grey? Use bg_subtle. If no token fits, the design is
    wrong — don't add a token to paper over it.
  * Disabled state is text_disabled on bg_subtle. Never Qt widget opacity;
    it causes repaint artifacts.

--------------------------------------------------------------------------
LIGHT / DARK (added 2026-07-29)
--------------------------------------------------------------------------

There are now two palettes with IDENTICAL key sets, plus geometry tokens that
are theme-independent. The important part is how the active one is published:

  ** TOKENS is MUTATED IN PLACE and must NEVER be rebound. **

`set_palette()` does `TOKENS.update(...)`, not `TOKENS = ...`. That is what
lets sessions_window/status_delegate.py and sessions_window/redaction_dialog.py
keep their module-level `from theme.tokens import TOKENS` and simply read
`TOKENS[key]` at paint time — they pick up a theme switch on the next repaint
with no code of their own and no re-creation of the delegate. Rebinding this
name (here or anywhere) would leave those two modules holding the old palette
forever, and the symptom is narrow and easy to miss: everything re-themes
except the session-table status pills and the redaction rubber band.

`update()` rather than `clear()` + `update()` is also deliberate. Both palettes
carry every key, so an update alone can never leave the dict in a state where a
lookup raises — and a paint that somehow interleaved would read a mix of two
palettes (ugly for one frame) instead of crashing on KeyError.

Adding a color token means adding it to BOTH palettes. `set_palette()` checks
that the key sets match, so a one-sided addition fails loudly rather than
rendering one theme with a literal "$my_new_token" string in the QSS.

Contrast: every foreground/background pair the QSS and the status delegate
actually produce was checked against WCAG AA (4.5:1 for text, 3:1 for disabled
text and for borders that carry meaning) when the dark palette was authored.
If you change a dark value, re-check the pair it participates in — the pills in
status_delegate.py (`accent` on `accent_subtle`, `danger` on `danger_subtle`,
`text_muted` on `bg_sunken`) are the tightest ones and the easiest to break.
"""

import logging

log = logging.getLogger(__name__)

THEMES = ("light", "dark")
DEFAULT_THEME = "light"

# Theme-INDEPENDENT. Geometry doesn't change between light and dark; putting it
# in both palettes would just be two places to forget.
_GEOMETRY = {
    "radius_control": "4px",
    "radius_panel":   "6px",
    "border_width":   "1px",
}

_LIGHT = {
    # Surfaces — bg_surface is the BRIGHTEST here: it's the elevated one
    # (panels, table body, inputs, the active tab) sitting on bg_window.
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
    # Surfaces. NOTE THE LADDER RUNS THE OPPOSITE WAY from light, and this is
    # correct, not a transcription error:
    #
    #   light:  window F4F5F7 > subtle EDEFF2 > sunken E4E7EB (each step
    #           DARKER = deeper), with surface FFFFFF brightest = elevated.
    #   dark:   window 191D23 < surface 22272E < subtle 2C333B < sunken 363E47
    #           (each step LIGHTER = deeper / more energy), which is how every
    #           dark UI signals elevation and hover — there is nothing darker
    #           than the window left to move toward.
    #
    # So in dark mode a button's :hover fill (bg_subtle) is LIGHTER than its
    # resting fill (bg_surface), and :pressed (bg_sunken) is lighter still.
    # "Fixing" that to match light mode's direction makes hover invisible.
    #
    # Not pure black, deliberately. Two reasons: #000 behind the bright
    # screenshots this app is full of is harsh, and — more usefully — it keeps
    # HANDOFF.md's debugging heuristic alive ("pure black in a Qt UI that has
    # no black token is almost always an unpainted region, not a border").
    # That heuristic only survives if no token is ever black.
    "bg_window":      "#191D23",
    "bg_surface":     "#22272E",
    "bg_subtle":      "#2C333B",
    "bg_sunken":      "#363E47",

    # Lines. 1.41:1 against bg_surface — enough to read as a border without the
    # graph-paper look a high-contrast border gives a dark UI.
    "border":         "#383F48",
    "border_strong":  "#4C5560",

    # Text. Off-white rather than #FFFFFF: pure white on a dark surface
    # produces visible halation around glyph stems at 10pt.
    "text":           "#E6EAEF",   # 12.44:1 on bg_surface
    "text_muted":     "#A3ADB8",   # 5.61:1 on bg_subtle
    # 3.27:1 on bg_subtle. WCAG exempts disabled controls from contrast
    # minimums, so this is a self-imposed floor, not a requirement — worth
    # knowing because the LIGHT palette's text_disabled sits at 2.20:1 and has
    # since v1.2.1. Left alone rather than "fixed" to match: changing a shipped
    # theme's colours is its own decision, not a side effect of adding dark mode.
    "text_disabled":  "#78828E",
    # DARK, not white. The dark accent is bright enough that white-on-accent
    # fails AA, so this token's two values are true opposites — which is also
    # why check.svg needs a per-theme variant: the tick is stroked in this
    # color and sits on the accent fill.
    "text_on_accent": "#0F1419",   # 7.01:1 on accent

    # Accent (single). BRIGHTER than the light accent, not darker. #2B5D8A on a
    # dark surface reads as a muddy hole rather than an accent, and it can't
    # carry legible text of either polarity.
    "accent":         "#62A5DE",   # 6.41:1 on bg_window, 5.69:1 on bg_surface
    "accent_hover":   "#7CB5E6",   # brighter on hover, matching the surface ladder
    # Deeper on press. No lighter step remains that stays distinguishable from
    # hover, and QSS has no inset shadow (the usual dark-mode pressed signal),
    # so dimming is what's available. It only has to differ from BOTH accent
    # and accent_hover, which it does.
    "accent_press":   "#4E92CB",
    "accent_subtle":  "#1E2E3E",   # 5.25:1 for accent text on it (status pill)
    "accent_focus":   "#85BAE8",   # focus border only

    # Danger (single). Same reasoning as accent — lightened so it works both as
    # a text color on the dark surfaces and as a fill.
    "danger":         "#E5706A",   # 4.91:1 on bg_surface, 4.71:1 on danger_subtle
    "danger_hover":   "#F08A84",
    "danger_subtle":  "#3A2422",
}

PALETTES = {
    "light": {**_LIGHT, **_GEOMETRY},
    "dark": {**_DARK, **_GEOMETRY},
}

# The ACTIVE palette. Read by theme/loader.py to substitute app.qss, and read
# directly at paint time by the two places that paint manually.
#
# Starts as light so that merely importing this module yields a usable palette:
# a failure to theme should never surface as a KeyError inside a paintEvent.
# main.py calls apply_theme() with the configured value before any window
# exists.
TOKENS = dict(PALETTES[DEFAULT_THEME])


def resolve_theme(name) -> str:
    """
    Normalise a configured theme name to one of THEMES.

    Fails SAFE rather than raising: an unrecognised value in config.json gets a
    log line and the light theme, matching how the rest of the app treats bad
    config (see publish/token.py, publish/tags.py). A typo'd theme name should
    not stop someone from recording.
    """
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
    """
    Make `name` the active palette by MUTATING TOKENS IN PLACE.

    Returns TOKENS — the same dict object every time, so callers must not
    assume they were handed a copy. See this module's docstring for why the
    in-place mutation is load-bearing rather than a shortcut.
    """
    theme = resolve_theme(name)
    incoming = PALETTES[theme]

    # A one-sided token addition would otherwise surface as a literal
    # "$token_name" in the rendered QSS for one theme only — visible as a
    # single unstyled widget, easy to miss, and hard to trace after the fact.
    # Fail at the point the mistake was made instead.
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


# Type. Lives here rather than in main.py so that the §11 audit grep
# ("setFont|setPointSize|font-size — only theme/ allowed") stays clean:
# theme/loader.py::apply_theme is the one place QApplication.setFont is called.
# 10pt ≈ 13px at 96 DPI, which is what the 28px row-height math in the style
# guide (§4, the descender rule) assumes. Not theme-dependent.
FONT_FAMILY = "Segoe UI"
FONT_SIZE_PT = 10
