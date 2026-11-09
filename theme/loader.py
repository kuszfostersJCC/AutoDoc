"""The only module allowed to call setStyleSheet(); applies app.qss, a matching
QPalette, and the app font to the QApplication.

apply_theme() is idempotent and may be re-called on the Qt main thread to switch
theme with windows open. It must be the only route: it rebuilds the palette as
well as the stylesheet and re-polishes live widgets, and skipping any of those
leaves the UI half-switched.
"""

import logging
import sys
from pathlib import Path
from string import Template

# Registers Qt's SVG image handler; without it every url(...svg) in app.qss
# silently resolves to nothing. Never remove this "unused" import.
from PySide6 import QtSvg  # noqa: F401
from PySide6.QtGui import QFont, QPixmapCache

from theme.palette import build_palette
from theme.tokens import (
    FONT_FAMILY,
    FONT_SIZE_PT,
    PALETTES,
    THEMES,
    TOKENS,
    resolve_theme,
    set_palette,
)

log = logging.getLogger(__name__)

# Fusion is applied once per process: re-calling setStyle() resets the palette and
# re-polishes the whole tree, making the ordering below load-bearing twice over.
_style_applied = False


def _theme_dir() -> Path:
    """Directory physically containing app.qss and icons/, source or frozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / "theme"
    return Path(__file__).resolve().parent


def _repolish_all(app) -> None:
    """Force every existing widget to re-evaluate the new stylesheet."""
    # Snapshotted into a list, and guarded per widget: unpolish/polish can trigger
    # layout work, and a widget torn down mid-pass must not abort the switch.
    for widget in list(app.allWidgets()):
        try:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
        except RuntimeError:
            continue


def apply_theme(app, name=None) -> str:
    """
    Apply `name` ("light" | "dark") to the QApplication; returns the theme actually
    applied. Safe to call repeatedly, from the Qt main thread only.
    """
    global _style_applied

    theme = resolve_theme(name)
    # Mutates theme.tokens.TOKENS in place, which is how the manual painters see
    # the new colours without being told anything.
    tokens = set_palette(theme)

    here = _theme_dir()
    qss_path = here / "app.qss"

    if not _style_applied:
        # Fusion is the only baseline that renders identically across Windows
        # versions; the native style mixed with heavy QSS draws partial widgets.
        app.setStyle("Fusion")
        _style_applied = True

    # One app font, set here and never per widget.
    font = QFont(FONT_FAMILY, FONT_SIZE_PT)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # Palette before stylesheet, and both are required; see theme/palette.py.
    app.setPalette(build_palette(tokens))

    values = dict(tokens)
    # Absolute, forward-slashed path for QSS url(). Per-theme subdirectory because
    # SVG cannot inherit a Qt palette colour.
    values["icon_dir"] = (here / "icons" / theme).as_posix()

    try:
        template = Template(qss_path.read_text(encoding="utf-8"))
    except OSError:
        # Non-fatal but loud: Fusion plus the palette above is ugly yet usable, and
        # refusing to launch over a cosmetic file would be worse.
        log.exception("apply_theme: could not read %s — running unstyled", qss_path)
        return theme

    # safe_substitute, not substitute or str.format: QSS is full of {} braces, and
    # an unknown $name should render as one visibly unstyled widget, not raise.
    app.setStyleSheet(template.safe_substitute(values))

    # Invalidate cached rasterisations before re-polishing, so the repolish pass
    # re-fetches icons instead of re-using what it just dropped.
    QPixmapCache.clear()
    _repolish_all(app)

    log.info("apply_theme: applied theme=%s (%s, icon_dir=%s)",
             theme, qss_path, values["icon_dir"])
    return theme


def current_theme() -> str:
    """The theme currently reflected in TOKENS, derived by comparing one sentinel value."""
    for theme in THEMES:
        if TOKENS["bg_window"] == PALETTES[theme]["bg_window"]:
            return theme
    return resolve_theme(None)
