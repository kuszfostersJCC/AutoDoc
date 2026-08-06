"""
theme/loader.py — the ONLY module in AutoDoc allowed to call setStyleSheet().

Applies theme/app.qss + a matching QPalette + the app font to the
QApplication, substituting theme/tokens.py values into $placeholders.

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
   it (see apply_theme's explicit failure below).

--------------------------------------------------------------------------
LIVE THEME SWITCHING (added 2026-07-29)
--------------------------------------------------------------------------

The old contract was "call load_theme() exactly once, before any window is
created". That is GONE, replaced by: apply_theme() is idempotent and safe to
call again at any time on the Qt main thread, including with windows open.
Settings → Appearance calls it directly so the switch is immediate.

Three things make re-application actually work, and all three are required —
omitting any one produces a half-switched UI rather than an error:

  1. The palette AND the stylesheet are both rebuilt from the same tokens.
     See theme/palette.py's docstring for why the palette is not optional:
     ~40 QSS rules say `background: transparent` and inherit whatever the
     palette paints underneath them.

  2. Every live widget is unpolished and re-polished. Qt does NOT re-evaluate
     a stylesheet against already-shown widgets on its own — the same
     mechanism thumb_card.py::set_marked() needs for its one dynamic
     property, applied to the whole tree. Without it, setStyleSheet appears
     to do nothing at all to windows that are already open.

  3. QPixmapCache is cleared, because QSS `url(...)` images are rasterised
     and cached. The two themes use different icon PATHS (icons/light vs
     icons/dark), which makes a stale hit unlikely rather than impossible —
     the clear is cheap insurance against a chevron that keeps its old
     colour. Nothing AutoDoc owns lives in that cache (thumbnail QPixmaps are
     held by ThumbCard directly), so clearing it cannot drop anything needed.

MAIN THREAD ONLY. This touches widgets, so it is bound by HANDOFF.md invariant
#1 exactly like any other UI code: from a worker thread, go through
ui_service.call_soon().
"""

import logging
import sys
from pathlib import Path
from string import Template

# Importing QtSvg registers Qt's SVG image handler. Without it every
# url(...svg) in app.qss silently resolves to nothing and the combo-box
# arrows/checkboxes just... have no glyph. Never remove this "unused" import.
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

# Fusion is applied once per process. Re-calling setStyle() on every switch
# would also work, but it resets the palette (making the ordering below
# load-bearing in a second, less obvious way) and re-polishes the whole tree as
# a side effect, which would make the explicit repolish pass below look
# redundant when it isn't.
_style_applied = False


def _theme_dir() -> Path:
    """Directory physically containing app.qss and icons/, source or frozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / "theme"
    return Path(__file__).resolve().parent


def _repolish_all(app) -> None:
    """
    Force every existing widget to re-evaluate the new stylesheet.

    app.allWidgets() is snapshotted into a list first: unpolish/polish can
    trigger layout and paint work, and iterating Qt's live widget registry
    while that happens risks visiting a widget mid-destruction. The per-widget
    guard is there for the same reason — one widget torn down during the pass
    must not abort the theme switch for every widget after it.
    """
    for widget in list(app.allWidgets()):
        try:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
        except RuntimeError:
            # The underlying C++ object went away between the snapshot and now.
            continue


def apply_theme(app, name=None) -> str:
    """
    Apply `name` ("light" | "dark") to the QApplication; returns the theme
    actually applied.

    Safe to call repeatedly, including with windows open — see this module's
    docstring. An unrecognised name falls back to light with a log line rather
    than raising (theme/tokens.py::resolve_theme).

    Fusion is non-negotiable: it's the only baseline that renders identically
    across Windows versions, and mixing the native Windows style with heavy
    QSS is what produces half-drawn/partial widget outlines.
    """
    global _style_applied

    theme = resolve_theme(name)
    # Mutates theme.tokens.TOKENS IN PLACE — which is how status_delegate.py
    # and redaction_dialog.py see the new colours without being told anything.
    tokens = set_palette(theme)

    here = _theme_dir()
    qss_path = here / "app.qss"

    if not _style_applied:
        app.setStyle("Fusion")
        _style_applied = True

    # Font is set here and nowhere else — never per widget. Inconsistent button
    # text was a top complaint in the old UI and it came from per-widget
    # setFont calls; there is one app font plus the type roles in app.qss.
    # Re-setting it on a theme switch is harmless (the font isn't
    # theme-dependent) and keeps this function a single complete description of
    # "how the app looks" rather than a partial one.
    font = QFont(FONT_FAMILY, FONT_SIZE_PT)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # Palette BEFORE stylesheet. Both must happen; see theme/palette.py.
    app.setPalette(build_palette(tokens))

    values = dict(tokens)
    # QSS url() needs a resolvable path with forward slashes, absolute so it
    # doesn't depend on the process working directory (which differs between
    # a source run and a double-clicked exe). Per-theme subdirectory: the two
    # themes need differently-coloured chevrons and checkmarks, and SVG has no
    # way to inherit a Qt palette colour.
    values["icon_dir"] = (here / "icons" / theme).as_posix()

    try:
        template = Template(qss_path.read_text(encoding="utf-8"))
    except OSError:
        # Loud, not silent. An unstyled app is a confusing bug report
        # ("it looks like the old version?"); a log line naming the missing
        # path is a 10-second fix. The app still runs — Fusion alone is ugly
        # but functional, and refusing to launch over a cosmetic file would
        # be worse for a tool people use to get work done. Note the palette
        # above HAS been applied, so a dark theme still reads as dark here
        # rather than as a light/dark mixture.
        log.exception("apply_theme: could not read %s — running unstyled", qss_path)
        return theme

    app.setStyleSheet(template.safe_substitute(values))

    # Order matters: invalidate cached rasterisations, THEN re-polish, so the
    # repolish pass re-fetches icons instead of re-using what it just dropped.
    QPixmapCache.clear()
    _repolish_all(app)

    log.info("apply_theme: applied theme=%s (%s, icon_dir=%s)",
             theme, qss_path, values["icon_dir"])
    return theme


def current_theme() -> str:
    """
    The theme currently reflected in TOKENS.

    Derived rather than stored: a second source of truth for "which theme is
    active" is exactly the kind of thing that drifts out of step with the one
    that actually drives painting. Identity comparison against PALETTES would
    be wrong (TOKENS is a copy, mutated in place), so this compares one
    sentinel value that differs between the two palettes.
    """
    for theme in THEMES:
        if TOKENS["bg_window"] == PALETTES[theme]["bg_window"]:
            return theme
    return resolve_theme(None)
