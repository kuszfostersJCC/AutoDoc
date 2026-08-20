"""Table model over the session dicts from sessions_window._load_sessions().

Rows are held verbatim; session_at(row) hands the original dict back to callers.
"""

import logging
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

log = logging.getLogger(__name__)

# STATUS is painted by StatusDelegate.
COL_NAME, COL_DURATION, COL_SCREENSHOTS, COL_SEGMENTS, COL_STATUS = range(5)

_HEADERS = {
    COL_NAME: "Session name",
    COL_DURATION: "Duration",
    COL_SCREENSHOTS: "Screenshots",
    COL_SEGMENTS: "Transcript segments",
    COL_STATUS: "Status",
}

# Cells and headers both read this mapping so their alignment cannot drift.
_ALIGNMENT = {
    COL_NAME: Qt.AlignLeft | Qt.AlignVCenter,
    COL_DURATION: Qt.AlignRight | Qt.AlignVCenter,
    COL_SCREENSHOTS: Qt.AlignRight | Qt.AlignVCenter,
    COL_SEGMENTS: Qt.AlignRight | Qt.AlignVCenter,
    COL_STATUS: Qt.AlignLeft | Qt.AlignVCenter,
}


class SessionsModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return row["name"]
            if col == COL_DURATION:
                return row["duration"] or "—"
            if col == COL_SCREENSHOTS:
                return str(row["screenshot_count"])
            if col == COL_SEGMENTS:
                return str(row["segment_count"])
            if col == COL_STATUS:
                return row["status"]
            return None

        if role == Qt.TextAlignmentRole:
            return int(_ALIGNMENT[col])

        if role == Qt.ToolTipRole:
            # The name column elides on narrow windows; the tooltip keeps the
            # full name reachable.
            if col == COL_NAME:
                return row["name"]
            return None

        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return _HEADERS.get(section)
        if role == Qt.TextAlignmentRole:
            return int(_ALIGNMENT.get(section, Qt.AlignLeft | Qt.AlignVCenter))
        return None

    def set_sessions(self, sessions: list[dict]) -> None:
        """Replace all rows. Full reset — the list is small (tens of items)."""
        self.beginResetModel()
        self._rows = list(sessions)
        self.endResetModel()

    def session_at(self, row: int) -> Optional[dict]:
        """The original session dict for a view row, or None if out of range."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None
