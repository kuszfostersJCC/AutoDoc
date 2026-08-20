"""Builds a QPalette from the active theme tokens; theme/loader.py is the only caller.

The palette is not optional: ~40 QSS rules say `background: transparent` and inherit
whatever the palette paints underneath, so the stylesheet and the palette must come
from the same tokens and be applied together, or a theme renders in patches.
"""

from PySide6.QtGui import QColor, QPalette


def build_palette(tokens: dict) -> QPalette:
    """Map theme tokens onto a QPalette. `tokens` is read only."""
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    def c(key: str) -> QColor:
        return QColor(tokens[key])

    pal = QPalette()
    surface = c("bg_surface")

    # All three real colour groups are written, with the genuine Inactive/Disabled
    # differences overridden below. Qt falls back to the DEFAULT (light) palette for
    # any group left unset, so a dark theme would go light when a window loses focus.
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
        # Same way round as app.qss's QToolTip rule, so a tooltip Fusion draws
        # itself cannot disagree with one the stylesheet draws.
        role.ToolTipBase:     c("text"),
        role.ToolTipText:     surface,
        # Unused by our QSS, but Fusion reaches for it on some pressed states and
        # its default is a red belonging to neither palette.
        role.BrightText:      c("danger"),
        # The 3D roles are approximations derived from bg_surface: they are only
        # reached by native Fusion drawing the stylesheet does not override.
        role.Light:           surface.lighter(140),
        role.Midlight:        surface.lighter(120),
        role.Mid:             c("border"),
        role.Dark:            c("border_strong"),
        role.Shadow:          surface.darker(200),
    }
    for r, color in pairs.items():
        for g in groups:
            pal.setColor(g, r, color)

    # An unfocused window's selection, matching app.qss's QTableView::item:selected:!active.
    pal.setColor(group.Inactive, role.Highlight, c("accent_subtle"))
    pal.setColor(group.Inactive, role.HighlightedText, c("text"))

    # text_disabled on bg_subtle, per the tokens module's rule. Never widget
    # opacity — that causes repaint artifacts.
    for r in (role.WindowText, role.Text, role.ButtonText, role.HighlightedText):
        pal.setColor(group.Disabled, r, c("text_disabled"))
    pal.setColor(group.Disabled, role.Base, c("bg_subtle"))
    pal.setColor(group.Disabled, role.Button, c("bg_subtle"))
    pal.setColor(group.Disabled, role.Highlight, c("bg_sunken"))
    pal.setColor(group.Disabled, role.PlaceholderText, c("text_disabled"))

    return pal
