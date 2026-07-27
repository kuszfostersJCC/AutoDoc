"""
sessions_window/redaction_dialog.py — full-size preview + redaction editor (PRD F3).

Drag on the image to add a pixelation box over sensitive areas (student data,
IDs, emails). This is the FERPA-facing feature; treat its correctness as more
important than its looks.

TWO THINGS THAT MUST NOT DRIFT:

1. **Regions are stored in IMAGE-space pixels, not display-space.** The preview
   is downscaled to fit the screen, so every rectangle the user drags must be
   divided by that scale ratio before being appended to
   `event["data"]["redact_regions"]`. Store display coordinates by mistake and
   the redaction lands in the wrong place at export time — on a smaller area
   than intended, i.e. it silently fails to cover the sensitive data while
   still looking correct in this dialog. The raw PNG on disk is never modified;
   `recorder/screenshot.py::apply_redactions()` applies these regions at every
   render, in every mode including raw.

2. **The event dict is mutated in place, deliberately.** It's the same object
   that lives inside `session_data["events"]`, so the editor's existing "Save
   changes" button persists these edits with no extra plumbing. Do not
   deep-copy the event on the way in "for safety" — that silently breaks
   persistence, and the dialog will appear to work perfectly until you reopen
   the session.
"""

import logging
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sessions_window.image_bridge import pil_to_qpixmap, scaled_to_fit
from theme.tokens import TOKENS

log = logging.getLogger(__name__)

# Preview fits inside this box (logical px) without upscaling.
_MAX_W, _MAX_H = 1100, 700
# Drags smaller than this are treated as a stray click, not a region.
_MIN_DRAG_PX = 5


class _RedactionCanvas(QWidget):
    """
    Paints a pixmap and lets the user drag out rectangles on it.

    A plain QWidget with a custom paintEvent rather than a QLabel: the
    in-progress rubber band has to be drawn over the image every frame, and
    QLabel's pixmap painting doesn't compose with that cleanly.
    """

    def __init__(
        self,
        pixmap: QPixmap,
        logical_size: QSize,
        on_region: Callable[[QRect], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._on_region = on_region
        self._origin: Optional[QPoint] = None
        self._rubber: Optional[QRect] = None
        # LOGICAL size, passed in explicitly — NOT pixmap.size(). The pixmap is
        # rasterised at physical resolution for high-DPI crispness, so
        # pixmap.size() is dpr times larger than the widget should be. Sizing
        # the canvas from it would make the widget (and therefore every mouse
        # coordinate) disagree with the drawn image by the scale factor, which
        # would put every redaction box in the wrong place on a scaled display.
        self.setFixedSize(logical_size)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Swap the image. Size is unchanged — same source, same scale."""
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        if self._rubber is not None:
            pen = QPen(QColor(TOKENS["danger"]))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._rubber.normalized())
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._rubber = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._rubber = QRect(self._origin, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        rect = QRect(self._origin, event.position().toPoint()).normalized()
        self._origin = None
        self._rubber = None
        self.update()
        if rect.width() >= _MIN_DRAG_PX and rect.height() >= _MIN_DRAG_PX:
            self._on_region(rect)


class RedactionDialog(QDialog):
    """
    Full-size screenshot preview with drag-to-pixelate editing.

    Args:
        event:        the screenshot event dict — MUTATED in place (see module
                      docstring); this is how Save persists the change.
        render:       callable() -> PIL.Image for the event at the current
                      render mode, re-invoked after every edit so the preview
                      shows the real pipeline output rather than a fake overlay.
        render_mode:  current mode name, for the window title.
    """

    def __init__(self, event: dict, render: Callable[[], object], render_mode: str, parent=None) -> None:
        super().__init__(parent)
        self._event = event
        self._render = render
        self._dirty = False

        filename = event.get("data", {}).get("filename", "")
        self.setWindowTitle(f"Screenshot — {filename} ({render_mode})")

        base = self._render_pixmap_full()
        if base is None or base.isNull():
            raise ValueError("could not render screenshot for redaction")

        # Rasterise at physical resolution so the preview is crisp on a scaled
        # display (see image_bridge.scaled_to_fit), then derive the LOGICAL
        # on-screen size from it. Every coordinate below — widget size, mouse
        # positions, and therefore self._ratio — is logical; mixing in the
        # physical pixmap size here is what would silently offset every
        # redaction box on a high-DPI monitor.
        self._dpr = max(self.devicePixelRatioF(), 1.0)
        shown = scaled_to_fit(base, _MAX_W, _MAX_H, self._dpr)
        self._logical_size = QSize(
            max(round(shown.width() / self._dpr), 1),
            max(round(shown.height() / self._dpr), 1),
        )
        # ratio maps image-space → logical display-space; divide to convert back.
        self._ratio = (
            self._logical_size.width() / base.width() if base.width() > 0 else 1.0
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        hint = QLabel(
            "Drag on the image to pixelate sensitive areas (student data, IDs, "
            "emails). Changes apply to previews, exports, and generated documents."
        )
        hint.setProperty("role", "caption")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._canvas = _RedactionCanvas(shown, self._logical_size, self._add_region, self)
        lay.addWidget(self._canvas, 0, Qt.AlignmentFlag.AlignCenter)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)
        clear_btn = QPushButton("Clear all boxes")
        clear_btn.setProperty("variant", "danger")
        clear_btn.clicked.connect(self._clear_all)
        bar.addWidget(clear_btn)
        bar.addStretch(1)
        undo_btn = QPushButton("Undo last box")
        undo_btn.clicked.connect(self._undo_last)
        bar.addWidget(undo_btn)
        bar.addSpacing(16)
        done_btn = QPushButton("Done")
        done_btn.setProperty("variant", "primary")
        done_btn.clicked.connect(self.accept)
        bar.addWidget(done_btn)
        lay.addLayout(bar)

    # ------------------------------------------------------------------ #
    # Rendering                                                            #
    # ------------------------------------------------------------------ #

    def _render_pixmap_full(self) -> Optional[QPixmap]:
        img = self._render()
        if img is None:
            return None
        return pil_to_qpixmap(img)

    def _refresh(self) -> None:
        """Re-render through the real pipeline so the preview is truthful."""
        base = self._render_pixmap_full()
        if base is None or base.isNull():
            return
        # Same target box and dpr as the constructor, so the logical size (and
        # therefore self._ratio) stays valid across refreshes.
        self._canvas.set_pixmap(scaled_to_fit(base, _MAX_W, _MAX_H, self._dpr))

    # ------------------------------------------------------------------ #
    # Region editing                                                       #
    # ------------------------------------------------------------------ #

    def _add_region(self, rect: QRect) -> None:
        """Convert a display-space rect to image space and record it."""
        if self._ratio <= 0:
            return
        region = [
            int(rect.left() / self._ratio),
            int(rect.top() / self._ratio),
            int(rect.width() / self._ratio),
            int(rect.height() / self._ratio),
        ]
        self._event.setdefault("data", {}).setdefault("redact_regions", []).append(region)
        self._dirty = True
        self._refresh()

    def _undo_last(self) -> None:
        regions = self._event.get("data", {}).get("redact_regions") or []
        if regions:
            regions.pop()
            self._dirty = True
            self._refresh()

    def _clear_all(self) -> None:
        if self._event.get("data", {}).get("redact_regions"):
            self._event["data"]["redact_regions"] = []
            self._dirty = True
            self._refresh()

    def is_dirty(self) -> bool:
        """True if any region was added/removed — caller rebuilds thumbnails."""
        return self._dirty
