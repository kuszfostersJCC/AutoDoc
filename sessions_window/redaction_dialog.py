"""Full-size screenshot editor: browse, drag-to-pixelate, drag-to-crop.

Redaction and crop rectangles are stored in IMAGE-space pixels, not display-space.
Every per-image value (logical size, ratio, crop offset) is rebuilt in _load().
The event dict is mutated in place, so the editor's Save persists these edits.
"""

import logging
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from recorder.screenshot import MIN_CROP_PX, event_crop, event_note, normalize_crop
from sessions_window.image_bridge import pil_to_qpixmap, scaled_to_fit
from theme.tokens import TOKENS

log = logging.getLogger(__name__)

# Preview fits inside this box (logical px) without upscaling.
_MAX_W, _MAX_H = 1100, 700
# Drags smaller than this are treated as a stray click, not a region.
_MIN_DRAG_PX = 5
# Crop handle: drawn side length, and the larger radius that counts as a hit.
_HANDLE_PX = 9
_HANDLE_HIT_PX = 14
MODE_REDACT = "redact"
MODE_CROP = "crop"


class _ImageCanvas(QWidget):
    """Paints a pixmap and lets the user drag rectangles on it.

    MODE_REDACT rubber-bands a new pixelation box; MODE_CROP drags the crop
    box's corner handles, its interior, or a fresh box from empty space.
    """

    def __init__(
        self,
        pixmap: QPixmap,
        logical_size: QSize,
        on_region: Callable[[QRect], None],
        on_crop_changed: Callable[[], None],
        on_step: Callable[[int], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._on_region = on_region
        self._on_crop_changed = on_crop_changed
        self._on_step = on_step
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._mode = MODE_REDACT
        self._origin: Optional[QPoint] = None
        self._rubber: Optional[QRect] = None
        self._crop: Optional[QRect] = None
        self._drag_anchor: Optional[QPoint] = None   # fixed corner while resizing
        self._move_from: Optional[QPoint] = None     # cursor origin while moving
        self._move_rect: Optional[QRect] = None      # crop at the start of a move
        # LOGICAL size, not pixmap.size(): the pixmap is rasterised at physical
        # resolution, so sizing from it would scale every mouse coordinate away
        # from the drawn image.
        self.setFixedSize(logical_size)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- state ---------------------------------------------------------- #

    def set_image(self, pixmap: QPixmap, logical_size: QSize) -> None:
        """Swap the image and its logical size; sibling screenshots differ in size."""
        self._pixmap = pixmap
        self._origin = None
        self._rubber = None
        self.setFixedSize(logical_size)
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._origin = None
        self._rubber = None
        self._drag_anchor = None
        self._move_from = None
        self.setCursor(
            Qt.CursorShape.ArrowCursor if mode == MODE_CROP else Qt.CursorShape.CrossCursor
        )
        self.update()

    def set_crop_rect(self, rect: Optional[QRect]) -> None:
        self._crop = QRect(rect) if rect is not None else None
        self.update()

    def crop_rect(self) -> Optional[QRect]:
        return QRect(self._crop) if self._crop is not None else None

    def keyPressEvent(self, event) -> None:
        """Left/Right step through the session. Handled here rather than by a
        QShortcut so the note field keeps the arrows while it has focus."""
        if event.key() == Qt.Key.Key_Left:
            self._on_step(-1)
        elif event.key() == Qt.Key.Key_Right:
            self._on_step(1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- painting ------------------------------------------------------- #

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)

        if self._mode == MODE_CROP:
            self._paint_crop(painter)
        elif self._rubber is not None:
            pen = QPen(QColor(TOKENS["danger"]))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._rubber.normalized())

        painter.end()

    def _paint_crop(self, painter: QPainter) -> None:
        if self._crop is None:
            return
        crop = self._crop.normalized()
        full = self.rect()

        # Scrim over everything the crop discards; painted rather than styled
        # because it must read the same over a light and a dark screenshot.
        scrim = QColor(0, 0, 0, 110)
        painter.fillRect(QRect(full.left(), full.top(), full.width(), crop.top() - full.top()), scrim)
        painter.fillRect(QRect(full.left(), crop.bottom() + 1, full.width(), full.bottom() - crop.bottom()), scrim)
        painter.fillRect(QRect(full.left(), crop.top(), crop.left() - full.left(), crop.height()), scrim)
        painter.fillRect(QRect(crop.right() + 1, crop.top(), full.right() - crop.right(), crop.height()), scrim)

        accent = QColor(TOKENS["accent"])
        pen = QPen(accent)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(crop)

        painter.setBrush(accent)
        painter.setPen(Qt.PenStyle.NoPen)
        half = _HANDLE_PX // 2
        for corner in self._corners(crop):
            painter.drawRect(
                QRect(corner.x() - half, corner.y() - half, _HANDLE_PX, _HANDLE_PX)
            )

    @staticmethod
    def _corners(rect: QRect) -> list:
        return [rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()]

    # -- mouse ---------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = self._clamped(event.position().toPoint())
        if self._mode == MODE_CROP:
            self._crop_press(pos)
        else:
            self._origin = pos
            self._rubber = QRect(pos, pos)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = self._clamped(event.position().toPoint())
        if self._mode == MODE_CROP:
            self._crop_move(pos)
        elif self._origin is not None:
            self._rubber = QRect(self._origin, pos)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = self._clamped(event.position().toPoint())

        if self._mode == MODE_CROP:
            self._crop_move(pos)
            was_dragging = self._drag_anchor is not None or self._move_from is not None
            self._drag_anchor = None
            self._move_from = None
            self._move_rect = None
            self.update()
            if was_dragging:
                self._on_crop_changed()
            return

        if self._origin is None:
            return
        rect = QRect(self._origin, pos).normalized()
        self._origin = None
        self._rubber = None
        self.update()
        if rect.width() >= _MIN_DRAG_PX and rect.height() >= _MIN_DRAG_PX:
            self._on_region(rect)

    def _clamped(self, point: QPoint) -> QPoint:
        """Keep coordinates inside the image; Qt delivers positions outside it mid-drag."""
        return QPoint(
            max(0, min(point.x(), self.width() - 1)),
            max(0, min(point.y(), self.height() - 1)),
        )

    def _crop_press(self, pos: QPoint) -> None:
        crop = self._crop.normalized() if self._crop is not None else None
        if crop is not None:
            for corner in self._corners(crop):
                if (abs(corner.x() - pos.x()) <= _HANDLE_HIT_PX
                        and abs(corner.y() - pos.y()) <= _HANDLE_HIT_PX):
                    # Anchoring the opposite corner lets a handle dragged past
                    # it flip cleanly instead of yielding a negative-width rect.
                    self._drag_anchor = QPoint(
                        crop.right() if corner.x() == crop.left() else crop.left(),
                        crop.bottom() if corner.y() == crop.top() else crop.top(),
                    )
                    self._crop = QRect(self._drag_anchor, pos)
                    return
            if crop.contains(pos):
                self._move_from = pos
                self._move_rect = QRect(crop)
                return
        self._drag_anchor = pos
        self._crop = QRect(pos, pos)

    def _crop_move(self, pos: QPoint) -> None:
        if self._drag_anchor is not None:
            self._crop = QRect(self._drag_anchor, pos).normalized()
        elif self._move_from is not None and self._move_rect is not None:
            dx = pos.x() - self._move_from.x()
            dy = pos.y() - self._move_from.y()
            moved = QRect(self._move_rect)
            moved.translate(dx, dy)
            # Slide back inside the image rather than shrinking at the edge.
            moved.moveLeft(max(0, min(moved.left(), self.width() - moved.width())))
            moved.moveTop(max(0, min(moved.top(), self.height() - moved.height())))
            self._crop = moved


class RedactionDialog(QDialog):
    """Full-size screenshot editor with arrow-key navigation across the session.

    Args:
        events: ordered screenshot event dicts, mutated in place.
        render: callable(event, with_crop: bool) -> PIL.Image, re-invoked after
            every edit.

    Raises ValueError if the screenshot it opens on cannot be rendered; a failure
    while navigating is not fatal.
    """

    def __init__(
        self,
        events: list,
        index: int,
        render: Callable[[dict, bool], object],
        render_mode: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if not events:
            raise ValueError("no screenshots to show")
        self._events = events
        self._index = max(0, min(index, len(events) - 1))
        self._render = render
        self._render_mode = render_mode
        self._dirty = False
        self._crop_mode = False

        # Per-image geometry — every one of these is rebuilt by _load().
        self._dpr = max(self.devicePixelRatioF(), 1.0)
        self._logical_size = QSize(1, 1)
        self._ratio = 1.0
        self._offset = QPoint(0, 0)
        self._base_size = QSize(1, 1)

        self._build_ui()

        if not self._load():
            raise ValueError("could not render screenshot for editing")


    # ---- UI ---- #

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        self._hint = QLabel("")
        self._hint.setProperty("role", "caption")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        # Placeholder: _load() replaces the pixmap and the fixed size before show.
        self._canvas = _ImageCanvas(
            QPixmap(1, 1), QSize(1, 1), self._add_region, self._on_crop_dragged,
            self._step, self
        )
        lay.addWidget(self._canvas, 0, Qt.AlignmentFlag.AlignCenter)

        lay.addLayout(self._build_nav_bar())
        lay.addLayout(self._build_note_row())
        lay.addLayout(self._build_action_bar())

    def _build_nav_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 8, 0, 0)
        bar.setSpacing(8)

        self._prev_btn = QPushButton("← Previous")
        self._prev_btn.setProperty("variant", "quiet")
        self._prev_btn.clicked.connect(lambda: self._step(-1))
        bar.addWidget(self._prev_btn)

        self._counter = QLabel("")
        self._counter.setProperty("role", "caption")
        bar.addWidget(self._counter)

        self._next_btn = QPushButton("Next →")
        self._next_btn.setProperty("variant", "quiet")
        self._next_btn.clicked.connect(lambda: self._step(1))
        bar.addWidget(self._next_btn)

        bar.addStretch(1)
        return bar

    def _build_note_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(8)

        label = QLabel("Note:")
        label.setProperty("role", "caption")
        row.addWidget(label)

        self._note_edit = QLineEdit()
        self._note_edit.setObjectName("screenshotNote")
        self._note_edit.setPlaceholderText("Optional context for the AI")
        self._note_edit.textEdited.connect(self._on_note_edited)
        # Return means "done typing": hand focus back to the canvas, so the
        # arrow keys resume navigating instead of going nowhere.
        self._note_edit.returnPressed.connect(self._canvas.setFocus)
        row.addWidget(self._note_edit, 1)
        return row

    def _build_action_bar(self) -> QHBoxLayout:
        """Action bar; the redact-mode and crop-mode button sets share this row."""
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)

        self._clear_btn = QPushButton("Clear all boxes")
        self._clear_btn.setProperty("variant", "danger")
        self._clear_btn.clicked.connect(self._clear_all)
        bar.addWidget(self._clear_btn)

        self._remove_crop_btn = QPushButton("Remove crop")
        self._remove_crop_btn.setProperty("variant", "danger")
        self._remove_crop_btn.clicked.connect(self._remove_crop)
        bar.addWidget(self._remove_crop_btn)

        bar.addStretch(1)

        self._crop_btn = QPushButton("Crop…")
        self._crop_btn.clicked.connect(self._toggle_crop_mode)
        bar.addWidget(self._crop_btn)

        self._undo_btn = QPushButton("Undo last box")
        self._undo_btn.clicked.connect(self._undo_last)
        bar.addWidget(self._undo_btn)

        self._apply_crop_btn = QPushButton("Apply crop")
        self._apply_crop_btn.clicked.connect(self._apply_crop)
        bar.addWidget(self._apply_crop_btn)

        bar.addSpacing(16)

        done_btn = QPushButton("Done")
        done_btn.setProperty("variant", "primary")
        done_btn.clicked.connect(self.accept)
        bar.addWidget(done_btn)

        # Without this, Return in the note field activates a button and closes
        # the dialog.
        # NoFocus is load-bearing, not cosmetic: a focused nav button that gets
        # disabled at the end of the list hands focus to the note field, and the
        # arrow keys then silently edit text instead of navigating.
        for btn in (
            self._prev_btn,
            self._next_btn,
            self._clear_btn,
            self._remove_crop_btn,
            self._crop_btn,
            self._undo_btn,
            self._apply_crop_btn,
            done_btn,
        ):
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return bar

    # ---- Loading / geometry ---- #

    @property
    def _event(self) -> dict:
        return self._events[self._index]

    def _render_pixmap(self) -> Optional[QPixmap]:
        """Render the current event; crop mode wants the UNCROPPED image."""
        img = self._render(self._event, not self._crop_mode)
        if img is None:
            return None
        return pil_to_qpixmap(img)

    def _load(self) -> bool:
        """(Re)build everything that depends on the current image and mode.

        The only place _ratio, _logical_size and _offset are assigned.
        """
        base = self._render_pixmap()
        if base is None or base.isNull():
            # The hint is set after _sync_ui() so it survives it.
            self._sync_note_field()
            self._sync_ui()
            self._hint.setText("Could not render this screenshot — the file may be missing.")
            return False

        self._base_size = base.size()
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
        self._offset = self._compute_offset(base)

        self._canvas.set_mode(MODE_CROP if self._crop_mode else MODE_REDACT)
        self._canvas.set_image(shown, self._logical_size)
        if self._crop_mode:
            self._canvas.set_crop_rect(self._initial_crop_rect())
        else:
            self._canvas.set_crop_rect(None)

        self._sync_note_field()
        self._sync_ui()
        if not self._note_edit.hasFocus():
            self._canvas.setFocus()
        self.adjustSize()
        return True

    def _compute_offset(self, base: QPixmap) -> QPoint:
        """Where display-space (0,0) sits in the ORIGINAL image's coordinates.

        A stored crop is re-normalised rather than trusted, because
        normalize_crop() may clamp or reject it.
        """
        if self._crop_mode:
            return QPoint(0, 0)
        crop = event_crop(self._event)
        if not crop:
            return QPoint(0, 0)
        try:
            x, y, _w, _h = (int(v) for v in crop)
        except (TypeError, ValueError):
            return QPoint(0, 0)
        origin_x, origin_y = max(0, x), max(0, y)
        box = normalize_crop(
            crop, origin_x + base.width(), origin_y + base.height()
        )
        if box is None:
            return QPoint(0, 0)
        if box[2] == base.width() and box[3] == base.height():
            return QPoint(box[0], box[1])
        return QPoint(0, 0)

    def _initial_crop_rect(self) -> QRect:
        """The crop box to show on entering crop mode: the existing crop, or all of it."""
        full = QRect(0, 0, self._logical_size.width(), self._logical_size.height())
        if self._ratio <= 0:
            return full
        # Via normalize_crop() so the box shown is the one the renderer applies.
        box = normalize_crop(
            event_crop(self._event), self._base_size.width(), self._base_size.height()
        )
        if box is None:
            return full
        x, y, w, h = box
        rect = QRect(
            round(x * self._ratio),
            round(y * self._ratio),
            round(w * self._ratio),
            round(h * self._ratio),
        )
        return rect.intersected(full) if rect.isValid() else full

    def _sync_ui(self) -> None:
        total = len(self._events)
        filename = self._event.get("data", {}).get("filename", "")
        self.setWindowTitle(
            f"Screenshot {self._index + 1} of {total} — {filename} ({self._render_mode})"
        )
        self._counter.setText(f"{self._index + 1} of {total}")

        self._prev_btn.setEnabled(not self._crop_mode and self._index > 0)
        self._next_btn.setEnabled(not self._crop_mode and self._index < total - 1)
        self._crop_btn.setText("Cancel crop" if self._crop_mode else "Crop…")

        has_crop = bool(event_crop(self._event))
        self._clear_btn.setVisible(not self._crop_mode)
        self._undo_btn.setVisible(not self._crop_mode)
        self._apply_crop_btn.setVisible(self._crop_mode)
        self._remove_crop_btn.setVisible(self._crop_mode and has_crop)

        if self._crop_mode:
            self._hint.setText("Drag a corner to set the crop; drag inside to move it.")
        else:
            text = "Drag to pixelate sensitive areas. ← → moves between screenshots."
            if has_crop:
                text += " Cropped."
            self._hint.setText(text)

    # ---- Navigation ---- #

    def _step(self, delta: int) -> None:
        """Move to the previous/next screenshot; clamped, not wrapping."""
        if self._crop_mode:
            return
        target = self._index + delta
        if not 0 <= target < len(self._events):
            return
        self._index = target
        self._load()

    def keyPressEvent(self, event) -> None:
        # Escape cancels crop mode instead of closing the dialog.
        if event.key() == Qt.Key.Key_Escape and self._crop_mode:
            self._exit_crop_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- Redaction regions ---- #

    def _add_region(self, rect: QRect) -> None:
        """Convert a display-space rect to image space and record it; _offset keeps
        it correct on an already-cropped screenshot."""
        if self._ratio <= 0:
            return
        region = [
            int(rect.left() / self._ratio) + self._offset.x(),
            int(rect.top() / self._ratio) + self._offset.y(),
            int(rect.width() / self._ratio),
            int(rect.height() / self._ratio),
        ]
        self._event.setdefault("data", {}).setdefault("redact_regions", []).append(region)
        self._dirty = True
        self._load()

    def _undo_last(self) -> None:
        regions = self._event.get("data", {}).get("redact_regions") or []
        if regions:
            regions.pop()
            self._dirty = True
            self._load()

    def _clear_all(self) -> None:
        if self._event.get("data", {}).get("redact_regions"):
            self._event["data"]["redact_regions"] = []
            self._dirty = True
            self._load()

    # ---- Crop ---- #

    def _toggle_crop_mode(self) -> None:
        if self._crop_mode:
            self._exit_crop_mode()
        else:
            self._crop_mode = True
            self._load()

    def _exit_crop_mode(self) -> None:
        self._crop_mode = False
        self._load()

    def _on_crop_dragged(self) -> None:
        """The box moved; nothing is stored until Apply crop."""
        self._sync_ui()

    def _apply_crop(self) -> None:
        rect = self._canvas.crop_rect()
        if rect is None or self._ratio <= 0:
            return
        rect = rect.normalized()
        candidate = [
            round(rect.left() / self._ratio),
            round(rect.top() / self._ratio),
            round(rect.width() / self._ratio),
            round(rect.height() / self._ratio),
        ]
        # Same function the renderer uses, so an accepted crop is one it applies.
        box = normalize_crop(candidate, self._base_size.width(), self._base_size.height())
        if box is None:
            self._hint.setText(
                f"Crop must be at least {MIN_CROP_PX}×{MIN_CROP_PX} pixels."
            )
            return
        self._event.setdefault("data", {})["crop"] = list(box)
        self._dirty = True
        self._crop_mode = False
        self._load()

    def _remove_crop(self) -> None:
        data = self._event.get("data", {})
        if data.pop("crop", None) is not None:
            self._dirty = True
        self._crop_mode = False
        self._load()

    # ---- Result ---- #

    def is_dirty(self) -> bool:
        """True if any region, crop or note changed — caller rebuilds thumbnails."""
        return self._dirty

    # ---- Note ---- #

    def _on_note_edited(self, text: str) -> None:
        """Store on every keystroke; an empty note removes the key rather than storing ""."""
        data = self._event.setdefault("data", {})
        stripped = text.strip()
        if stripped:
            if data.get("note") == stripped:
                return
            data["note"] = stripped
        elif data.pop("note", None) is None:
            return
        self._dirty = True

    def _sync_note_field(self) -> None:
        """Show the current event's note without emitting edit signals."""
        self._note_edit.blockSignals(True)
        self._note_edit.setText(event_note(self._event))
        self._note_edit.blockSignals(False)

