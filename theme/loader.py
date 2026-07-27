"""
theme/loader.py — the ONLY module in AutoDoc allowed to call setStyleSheet().

Loads theme/app.qss once onto the QApplication, substituting theme/tokens.py
values into $placeholders.

Two things here are deliberate and easy to "fix" into a bug:

1. string.Template / safe_substitute, NOT str.format(). QSS is full of {}
   braces; str.format() raises on the first rule it meets. safe_substitute
   (rather than substitute) means an unknown $name in the QSS is left as
   literal text instead of raising — a missing token shows up as one
   unstyled widget you can see, not a crash on startup.

2. _theme_dir() resolves the asset directory for BOTH source and frozen
   runs. The style guide's reference loader uses Path(__file__).parent,
   which is correct from source but wrong once PyInstaller puts this module
   in the PYZ archive — app.qss and icons/ are physical files that live
   wherever the spec's `datas` entry put them (_internal/theme/), which is
   sys._MEIPASS-relative, not __file__-relative. Getting this wrong makes
   the app launch completely unstyled with no error, because
   safe_substitute happily produces an empty stylesheet from a missing file
   only if you also swallow the read error — which is why we DON'T swallow
   it (see load_theme's explicit failure below).
"""

import logging
import sys
from pathlib import Path
from string import Template

# Importing QtSvg registers Qt's SVG image handler. Without it every
# url(...svg) in app.qss silently resolves to nothing and the combo-box
# arrows/checkboxes just... have no glyph. Never remove this "unused" import.
from PySide6 import QtSvg  # noqa: F401
from PySide6.QtGui import QFont

from theme.tokens import FONT_FAMILY, FONT_SIZE_PT, TOKENS

log = logging.getLogger(__name__)


def _theme_dir() -> Path:
    """Directory physically containing app.qss and icons/, source or frozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / "theme"
    return Path(__file__).resolve().parent


def load_theme(app) -> None:
    """
    Apply Fusion + app.qss to the QApplication. Call exactly once, before any
    window is created.

    Fusion is non-negotiable: it's the only baseline that renders identically
    across Windows versions, and mixing the native Windows style with heavy
    QSS is what produces half-drawn/partial widget outlines.
    """
    here = _theme_dir()
    qss_path = here / "app.qss"

    # Font is set exactly once, here — never per widget. Inconsistent button
    # text was a top complaint in the old UI and it came from per-widget
    # setFont calls; there is one app font plus the type roles in app.qss.
    font = QFont(FONT_FAMILY, FONT_SIZE_PT)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    values = dict(TOKENS)
    # QSS url() needs a resolvable path with forward slashes, absolute so it
    # doesn't depend on the process working directory (which differs between
    # a source run and a double-clicked exe).
    values["icon_dir"] = (here / "icons").as_posix()

    app.setStyle("Fusion")

    try:
        template = Template(qss_path.read_text(encoding="utf-8"))
    except OSError:
        # Loud, not silent. An unstyled app is a confusing bug report
        # ("it looks like the old version?"); a log line naming the missing
        # path is a 10-second fix. The app still runs — Fusion alone is ugly
        # but functional, and refusing to launch over a cosmetic file would
        # be worse for a tool people use to get work done.
        log.exception("load_theme: could not read %s — running unstyled", qss_path)
        return

    app.setStyleSheet(template.safe_substitute(values))
    log.info("load_theme: applied %s (icon_dir=%s)", qss_path, values["icon_dir"])
