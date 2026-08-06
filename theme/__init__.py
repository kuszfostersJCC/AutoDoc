"""
theme/ — the ONLY place AutoDoc defines how the UI looks.

See pyside6-ui-style-guide.md (repo root) for the binding rules. The short
version, because violating these is a defect even when the result looks fine:

  * All QSS lives in theme/app.qss and is applied to QApplication by
    theme/loader.py::apply_theme().
  * No setStyleSheet() call exists anywhere outside theme/loader.py.
  * No hex color, font size, or radius is defined outside theme/tokens.py.
    Python may READ tokens for things QSS cannot do (e.g. painting inside a
    QStyledItemDelegate) but may never invent a new value.
  * Every spacing/size number comes from {0, 4, 8, 12, 16, 24, 32}.

Light/dark (2026-07-29): there are two palettes in theme/tokens.py and
apply_theme() may be called again at runtime to switch between them. A theme
switch must go through apply_theme() — it rebuilds the QPalette as well as the
stylesheet and re-polishes existing widgets, and skipping any of those leaves
the UI half-switched. Never re-apply by touching TOKENS directly.
"""

from theme.loader import apply_theme, current_theme
from theme.tokens import DEFAULT_THEME, PALETTES, THEMES, TOKENS, resolve_theme

__all__ = [
    "apply_theme",
    "current_theme",
    "resolve_theme",
    "DEFAULT_THEME",
    "PALETTES",
    "THEMES",
    "TOKENS",
]
