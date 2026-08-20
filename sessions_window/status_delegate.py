"""Paints the session table's status column as a coloured pill.

QSS cannot style a cell by its value, so colours are read from theme.tokens;
never hard-code one here.
"""

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from theme.tokens import TOKENS

log = logging.getLogger(__name__)

# Status → (background token, text token); unlisted statuses use _FALLBACK.
_STATUS_TOKENS = {
    "ready":             ("accent_subtle", "accent"),
    "transcript_failed": ("danger_subtle", "danger"),
    "recording":         ("bg_sunken", "text_muted"),
    "unknown":           ("bg_subtle", "text_muted"),
}
_FALLBACK = ("bg_subtle", "text_muted")

_STATUS_LABELS = {
    "ready": "Ready",
    "transcript_failed": "Transcript failed",
    "recording": "Recording",
    "unknown": "Unknown",
}

_PILL_HEIGHT = 20      # fits inside a 28px row with 4px above and below
_PILL_PAD_H = 8
_PILL_RADIUS = 4       # matches radius_control


class StatusDelegate(QStyledItemDelegate):
    """Paints the status column as a filled, rounded pill."""

    def paint(self, painter: QPainter, option, index) -> None:
        status = (index.data(Qt.DisplayRole) or "unknown").strip()
        bg_key, fg_key = _STATUS_TOKENS.get(status, _FALLBACK)
        label = _STATUS_LABELS.get(status, status)

        # Let the style draw selection/hover first so the QSS ::item rules stay
        # authoritative and the pill paints on top.
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        pill_w = text_w + (_PILL_PAD_H * 2)

        cell = option.rect
        # Matches the 8px horizontal cell padding QTableView::item uses in app.qss.
        pill = QRectF(
            cell.left() + _PILL_PAD_H,
            cell.center().y() - (_PILL_HEIGHT / 2) + 1,
            min(pill_w, max(cell.width() - (_PILL_PAD_H * 2), 0)),
            _PILL_HEIGHT,
        )

        path = QPainterPath()
        path.addRoundedRect(pill, _PILL_RADIUS, _PILL_RADIUS)
        painter.fillPath(path, QColor(TOKENS[bg_key]))

        painter.setPen(QColor(TOKENS[fg_key]))
        painter.drawText(
            pill.adjusted(_PILL_PAD_H, 0, -_PILL_PAD_H, 0),
            Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(label, Qt.ElideRight, int(pill.width()) - (_PILL_PAD_H * 2)),
        )
        painter.restore()

    def sizeHint(self, option, index):
        """Widens for the pill; height is left to the view's fixed row height."""
        base = super().sizeHint(option, index)
        base.setWidth(base.width() + (_PILL_PAD_H * 4))
        return base
