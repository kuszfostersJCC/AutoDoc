"""
theme/ — the ONLY place AutoDoc defines how the UI looks.

See pyside6-ui-style-guide.md (repo root) for the binding rules. The short
version, because violating these is a defect even when the result looks fine:

  * All QSS lives in theme/app.qss and is loaded exactly once, onto
    QApplication, by theme/loader.py::load_theme().
  * No setStyleSheet() call exists anywhere outside theme/loader.py.
  * No hex color, font size, or radius is defined outside theme/tokens.py.
    Python may READ tokens for things QSS cannot do (e.g. painting inside a
    QStyledItemDelegate) but may never invent a new value.
  * Every spacing/size number comes from {0, 4, 8, 12, 16, 24, 32}.
"""

from theme.loader import load_theme
from theme.tokens import TOKENS

__all__ = ["load_theme", "TOKENS"]
