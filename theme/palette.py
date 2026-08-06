"""
theme/palette.py — builds a QPalette from the active tokens.

WHY THIS EXISTS (added 2026-07-29, with dark mode)
--------------------------------------------------

Before dark mode, AutoDoc set `Fusion` and then a big stylesheet, and never
touched the palette at all. That worked only because Fusion's DEFAULT palette
is light and the app was light: everywhere the QSS said `background:
transparent` — and it says that about forty times, on QLabel, on quiet/danger
buttons, on splitter handles, scroll areas, scrollbar tracks, header views,
menu items — the pixels underneath came from Fusion's light palette and
happened to be right.

Flip the tokens to dark without flipping the palette and every one of those
places keeps painting light. The symptom is not "the app is still light"; it's
patchy — light rectangles behind labels, a light gutter between splitter panes,
light scrollbar tracks, light frames around anything Fusion still draws itself.
app.qss already documents the mirror-image version of this bug in light mode
(the tab-bar band rendering as raw black where nothing painted, lines 59-65 and
79-82); a wrong palette is the same class of defect with the polarity reversed.

So: the QSS and the QPalette must be derived from the SAME tokens, and both
must be re-applied together. theme/loader.py::apply_theme is the only caller.

The five "3D" roles (Light, Midlight, Mid, Dark, Shadow) are derived
arithmetically from bg_surface rather than given their own tokens. They are
only ever reached by native Fusion drawing that our stylesheet does not
override, so an approximation is the right level of effort — and inventing five
more tokens per theme to feed them would violate the style guide's "if no token
fits, the design is wrong" rule for values no designer will ever look at.
QColor.lighter()/darker() self-clamp at pure white/black, which is what lets
one expression produce sensible results for both a near-white and a near-black
surface.
"""

from PySide6.QtGui import QColor, QPalette


def build_palette(tokens: dict) -> QPalette:
    """
    Map theme tokens onto a QPalette.

    `tokens` is theme/tokens.py's TOKENS (or any dict with the same keys). Read
    only — this function never mutates it.
    """
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    def c(key: str) -> QColor:
        return QColor(tokens[key])

    pal = QPalette()
    surface = c("bg_surface")

    # ---- Roles that carry real meaning -------------------------------- #
    # Written into all three real colour groups, with the handful of genuine
    # Inactive/Disabled differences overridden afterwards. Setting only Active
    # is a classic source of "the window looks right until it loses focus" —
    # Qt falls back to the DEFAULT (light) palette for whichever group you skip,
    # so in dark mode an unfocused window would go light in patches.
    #
    # Enumerated explicitly rather than via QPalette.ColorGroup.All: `All` does
    # exist in Qt's C++ header and setColor() handles it, but whether PySide6
    # surfaces it is not something that can be verified without importing Qt,
    # and Qt cannot be imported in the Cowork sandbox (HANDOFF.md, "Verifying UI
    # work"). A wrong enum name here is an AttributeError during startup — the
    # app wouldn't launch at all — so this deliberately uses only the three
    # members that are unambiguously present in every Qt version.
    groups = (group.Active, group.Inactive, group.Disabled)

    pairs = {
        role.Window:          c("bg_window"),
        role.WindowText:      c("text"),
        role.Base:            surface,
        role.AlternateBase:   c("bg_subtle"),
        role.Text:            c("text"),
        role.Button:          surface,
        role.ButtonText:      c("text"),
        role.PlaceholderText: c("text_disabled"),
        role.Highlight:       c("accent"),
        role.HighlightedText: c("text_on_accent"),
        role.Link:            c("accent"),
        role.LinkVisited:     c("accent_press"),
        # Deliberately the same way round as app.qss's QToolTip rule
        # (background $text / color $bg_surface) so a tooltip Fusion draws
        # itself can't disagree with one the stylesheet draws. In dark mode
        # this inverts on its own into a light tooltip on dark chrome, which is
        # the correct convention — no special case needed.
        role.ToolTipBase:     c("text"),
        role.ToolTipText:     surface,
        # Qt's "text that reads against a dark fill". Nothing in AutoDoc's QSS
        # uses it, but Fusion reaches for it on some pressed/highlighted
        # states, and the default is a red that belongs to neither palette.
        role.BrightText:      c("danger"),
        # ---- 3D roles: derived, see module docstring ------------------- #
        role.Light:           surface.lighter(140),
        role.Midlight:        surface.lighter(120),
        role.Mid:             c("border"),
        role.Dark:            c("border_strong"),
        role.Shadow:          surface.darker(200),
    }
    for r, color in pairs.items():
        for g in groups:
            pal.setColor(g, r, color)

    # ---- Inactive: an unfocused window's selection --------------------- #
    # Matches app.qss's `QTableView::item:selected:!active` rule (accent_subtle
    # fill, normal text). Left at the Active values, a background window shows
    # a full-strength accent selection and reads as though it still has focus.
    pal.setColor(group.Inactive, role.Highlight, c("accent_subtle"))
    pal.setColor(group.Inactive, role.HighlightedText, c("text"))

    # ---- Disabled ------------------------------------------------------ #
    # text_disabled on bg_subtle, per the tokens module's rule. Explicitly NOT
    # done with widget opacity — that causes repaint artifacts.
    for r in (role.WindowText, role.Text, role.ButtonText, role.HighlightedText):
        pal.setColor(group.Disabled, r, c("text_disabled"))
    pal.setColor(group.Disabled, role.Base, c("bg_subtle"))
    pal.setColor(group.Disabled, role.Button, c("bg_subtle"))
    pal.setColor(group.Disabled, role.Highlight, c("bg_sunken"))
    pal.setColor(group.Disabled, role.PlaceholderText, c("text_disabled"))

    return pal
