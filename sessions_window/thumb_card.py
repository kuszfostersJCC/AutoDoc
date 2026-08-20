"""One screenshot thumbnail card in the session editor."""

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sessions_window.image_bridge import scaled_to_fit

log = logging.getLogger(__name__)

# Fixed card geometry, in logical pixels.
CARD_W, CARD_H = 240, 200
_IMAGE_MIN_H = 96


class ThumbCard(QFrame):
    """A bordered thumbnail card with a caption and a Delete/Restore toggle.

    Args:
        pixmap: the ORIGINAL, unscaled pixmap; kept so re-scaling never
            resamples an already-scaled copy.
    """

    def __init__(
        self,
        pixmap: QPixmap,
        caption: str,
        on_open: Callable[[], None],
        on_toggle: Callable[[bool], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("thumbCard")
        self.setFixedSize(CARD_W, CARD_H)

        self._original = pixmap
        self._on_open = on_open
        self._on_toggle = on_toggle
        self._marked = False

        lay = QVBoxLayout(self)
        # >= border_width + 1 so the child can never paint onto the border.
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self._img = QLabel(self)
        self._img.setObjectName("thumbImage")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setScaledContents(False)          # True ignores aspect ratio
        self._img.setMinimumSize(1, 1)
        self._img.setCursor(Qt.CursorShape.PointingHandCursor)
        # QLabel has no clicked signal, so its handler is assigned rather than
        # subclassed.
        self._img.mousePressEvent = self._image_clicked  # type: ignore[method-assign]
        lay.addWidget(self._img, 1)

        self._caption = QLabel(caption, self)
        self._caption.setObjectName("thumbCaption")
        self._caption.setProperty("role", "caption")
        self._caption.setWordWrap(False)
        self._caption.setTextFormat(Qt.TextFormat.PlainText)
        self._caption_text = caption
        lay.addWidget(self._caption)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self._del_btn = QPushButton("Delete", self)
        self._del_btn.setProperty("variant", "danger")
        self._del_btn.clicked.connect(self._toggle)
        actions.addWidget(self._del_btn)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._rescale()

    def _rescale(self) -> None:
        """Fit the original pixmap into the label's current content area."""
        area = self._img.contentsRect().size()
        w, h = area.width(), max(area.height(), _IMAGE_MIN_H)
        if w <= 0 or h <= 0 or self._original.isNull():
            return
        self._img.setPixmap(
            scaled_to_fit(self._original, w, h, self.devicePixelRatioF())
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()
        self._elide_caption()

    def _elide_caption(self) -> None:
        metrics = self._caption.fontMetrics()
        self._caption.setText(
            metrics.elidedText(
                self._caption_text, Qt.TextElideMode.ElideRight, self._caption.width()
            )
        )

    def _image_clicked(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open()

    def _toggle(self) -> None:
        self.set_marked(not self._marked)
        self._on_toggle(self._marked)

    def set_marked(self, marked: bool) -> None:
        """Mark/unmark for deletion.

        Qt does not re-evaluate property-based QSS selectors on a shown widget,
        so the unpolish/polish pair is required for #thumbCard[marked] to apply.
        """
        self._marked = marked
        self.setProperty("marked", "true" if marked else "false")
        self._del_btn.setText("Restore" if marked else "Delete")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_marked(self) -> bool:
        return self._marked

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Swap in a newly rendered original."""
        self._original = pixmap
        self._rescale()
