"""
sessions_window/thumb_card.py — one screenshot thumbnail in the session editor.

This widget is where the old UI's worst visual bug lived: "pictures overlap
their holding boxes and create a broken outline at the bottom". The cause was
one widget both drawing the border AND being sized by the pixmap, so the image
painted over its own border edge — and only on the axis where rounding went the
wrong way, which is why it showed up at the bottom only.

Style guide §8 rules, all load-bearing here:
  1. The CONTAINER draws the border; the image lives in a CHILD QLabel. Never
     both on one widget.
  2. The container has an explicit fixed size; the image is scaled to fit the
     CHILD's contentsRect, not the reverse.
  3. Container layout margins >= border_width + 1 → 8.
  4. setScaledContents(False), always. True ignores aspect ratio and overdraws.
  5. Scale with KeepAspectRatio + SmoothTransformation, re-derived from the
     ORIGINAL pixmap on every resize (never from an already-scaled copy).
  6. No border-radius on an image container (enforced in app.qss via
     #thumbCard { border-radius: 0 }).

The delete-marked appearance comes from the `marked` dynamic property plus the
#thumbCard[marked="true"] rule in app.qss — not from a local stylesheet and not
from a palette poke.
"""

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

# Fixed card geometry. Logical pixels; Qt handles DPI. The image area is what's
# left after 8px margins and the caption/action rows.
CARD_W, CARD_H = 240, 200
_IMAGE_MIN_H = 96


class ThumbCard(QFrame):
    """
    A bordered card containing a screenshot thumbnail, a caption, and a
    Delete/Restore toggle.

    Args:
        pixmap:    the ORIGINAL (unscaled) pixmap. Kept for re-scaling.
        caption:   one-line metadata text (timestamp • click type).
        on_open:   called with no args when the image is clicked.
        on_toggle: called with (marked: bool) when Delete/Restore is pressed.
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
        self._img.setScaledContents(False)          # §8.4 — never True
        self._img.setMinimumSize(1, 1)              # allow shrinking; §6
        self._img.setCursor(Qt.CursorShape.PointingHandCursor)
        # A click target on a QLabel needs an explicit event hook; QLabel has no
        # clicked signal. mousePressEvent on the label is assigned rather than
        # subclassed to keep this file to one widget class.
        self._img.mousePressEvent = self._image_clicked  # type: ignore[method-assign]
        lay.addWidget(self._img, 1)                 # the one stretching child

        self._caption = QLabel(caption, self)
        self._caption.setObjectName("thumbCaption")
        self._caption.setProperty("role", "caption")
        # One line, elided — never let a long caption widen the card (§6).
        self._caption.setWordWrap(False)
        self._caption.setTextFormat(Qt.TextFormat.PlainText)
        self._caption_text = caption
        lay.addWidget(self._caption)

        # Per-card action row follows the same action-bar rules as everywhere
        # else (§6): destructive left, stretch, nothing floated over the image.
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

    # ------------------------------------------------------------------ #
    # Painting / sizing                                                    #
    # ------------------------------------------------------------------ #

    def _rescale(self) -> None:
        """
        Fit the original pixmap into the label's current content area.

        devicePixelRatioF() is passed through so the thumbnail is rasterised at
        the display's real pixel density — without it, thumbnails on a scaled
        display are rendered at logical size and then upscaled by Qt, which
        looked noticeably softer than the source PNG. See image_bridge.
        """
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

    # ------------------------------------------------------------------ #
    # Interaction                                                          #
    # ------------------------------------------------------------------ #

    def _image_clicked(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open()

    def _toggle(self) -> None:
        self.set_marked(not self._marked)
        self._on_toggle(self._marked)

    def set_marked(self, marked: bool) -> None:
        """
        Mark/unmark for deletion.

        The visual change comes from #thumbCard[marked="true"] in app.qss. Qt
        does NOT re-evaluate property-based selectors on its own after a widget
        is shown, so the unpolish/polish pair is required — without it the
        property changes and nothing visibly happens (style guide §3).
        """
        self._marked = marked
        self.setProperty("marked", "true" if marked else "false")
        self._del_btn.setText("Restore" if marked else "Delete")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_marked(self) -> bool:
        return self._marked

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Swap in a newly rendered original (e.g. render mode changed)."""
        self._original = pixmap
        self._rescale()
