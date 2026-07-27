"""
sessions_window/status_delegate.py — status "pill" painter for the session table.

Why a delegate: a session's status is the one column where the value's meaning
matters at a glance ("transcript_failed" needs to be findable while scrolling).
QSS cannot style an individual cell by its value, so this is painted manually.

This is the sanctioned exception to "no colors in Python" (style guide §1.3):
painting is something QSS genuinely cannot do, so the delegate READS
theme.tokens. It defines no colors of its own — every value below comes from
TOKENS, and adding a literal hex string here would be a defect.

Geometry follows §4: the pill is centred in the 28px row, its height is fixed,
and no value is off the {0, 4, 8, 12, 16, 24, 32} spacing scale.
"""

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from theme.tokens import TOKENS

log = logging.getLogger(__name__)

# Status → (background token, text token). Anything not listed falls back to
# neutral, so an unrecognised status still renders legibly rather than blank.
_STATUS_TOKENS = {
    "ready":             ("accent_subtle", "accent"),
    "transcript_failed": ("danger_subtle", "danger"),
    "recording":         ("bg_sunken", "text_muted"),
    "unknown":           ("bg_subtle", "text_muted"),
}
_FALLBACK = ("bg_subtle", "text_muted")

# Human-facing labels. The raw values are snake_case internals; §3 requires
# sentence case in the UI.
_STATUS_LABELS = {
    "ready": "Ready",
    "transcript_failed": "Transcript failed",
    "recording": "Recording",
    "unknown": "Unknown",
}

_PILL_HEIGHT = 20      # fits inside a 28px row with 4px above and below
_PILL_PAD_H = 8        # horizontal padding inside the pill
_PILL_RADIUS = 4       # matches radius_control


class StatusDelegate(QStyledItemDelegate):
    """Paints the status column as a filled, rounded pill."""

    def paint(self, painter: QPainter, option, index) -> None:
        status = (index.data(Qt.DisplayRole) or "unknown").strip()
        bg_key, fg_key = _STATUS_TOKENS.get(status, _FALLBACK)
        label = _STATUS_LABELS.get(status, status)

        # Draw selection/hover background first so the pill sits on top of it
        # exactly as an unstyled cell's text would. Painting our own selection
        # fill here would duplicate (and drift from) the QSS rules for
        # ::item:selected / :selected:!active.
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        pill_w = text_w + (_PILL_PAD_H * 2)

        cell = option.rect
        # Left-aligned within the cell, honouring the same 8px horizontal cell
        # padding that QTableView::item uses in app.qss.
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
        """
        Width must accommodate the pill; height is left to the view's fixed
        28px row height (§4). Returning a taller hint here would fight
        setDefaultSectionSize and reintroduce the descender-clipping class of
        bug the fixed row height exists to prevent.
        """
        base = super().sizeHint(option, index)
        base.setWidth(base.width() + (_PILL_PAD_H * 4))
        return base
