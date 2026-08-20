"""Session review window: transcript edits, screenshot redaction and deletion.

Generation is handed off through the on_generate callback; this window does no
background work.
"""

import json
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from recorder.screenshot import (
    RENDER_MODES,
    event_is_legacy,
    event_note,
    render_event_image,
    resolve_render_mode,
)
from recorder.transcript import apply_transcript_edit, editable_transcript
from sessions_window.image_bridge import pil_to_qpixmap
from sessions_window.redaction_dialog import RedactionDialog
from sessions_window.thumb_card import ThumbCard

log = logging.getLogger(__name__)

_MIN_W, _MIN_H = 760, 480
_DEFAULT_W, _DEFAULT_H = 1040, 660

# Strong references: an editor with no live Python reference is garbage
# collected and the window disappears.
_open_editors: list["SessionEditorWindow"] = []


def open_editor_window(
    session_data: dict,
    session_dir: Path,
    config,
    on_generate: Callable[[dict, Path], None],
) -> None:
    """Open the session editor; must be called on the Qt main thread."""
    win = SessionEditorWindow(session_data, session_dir, config, on_generate)
    _open_editors.append(win)
    win.show()


class SessionEditorWindow(QMainWindow):
    THUMBS_PER_ROW = 3

    def __init__(
        self,
        session_data: dict,
        session_dir: Path,
        config,
        on_generate: Callable[[dict, Path], None],
    ) -> None:
        super().__init__()
        self.session_data = dict(session_data)
        self.session_dir = session_dir
        self.config = config
        self._on_generate_cb = on_generate

        self._cards: dict[str, ThumbCard] = {}   # event_id → card
        self._deleted_ids: set[str] = set()

        self._screenshot_events = self._collect_screenshots()
        self._all_legacy = self._compute_all_legacy()
        self._render_mode = resolve_render_mode(self.session_data, config)

        name = self.session_data.get("name") or self.session_data.get("session_id", "Session")
        self.setWindowTitle(f"AutoDoc — Edit: {name}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        self._build_ui()
        self._populate()

    # ---- Session data helpers ---- #

    def _collect_screenshots(self) -> list[dict]:
        return sorted(
            [e for e in self.session_data.get("events", []) if e.get("type") == "screenshot"],
            key=lambda e: e["timestamp_offset_ms"],
        )

    def _compute_all_legacy(self) -> bool:
        return bool(self._screenshot_events) and all(
            event_is_legacy(e) for e in self._screenshot_events
        )

    # ---- UI construction ---- #

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())
        root.addWidget(self._build_splitter(), 1)
        root.addLayout(self._build_action_bar())

        self._status = QStatusBar()
        self.setStatusBar(self._status)

    def _build_header(self) -> QWidget:
        header = QWidget()
        lay = QHBoxLayout(header)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        name = self.session_data.get("name") or self.session_data.get("session_id", "")
        title = QLabel(name)
        title.setProperty("role", "title")
        lay.addWidget(title)
        lay.addStretch(1)

        sid = QLabel(self.session_data.get("session_id", ""))
        sid.setProperty("role", "caption")
        lay.addWidget(sid)
        return header

    def _build_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_transcript_pane())
        splitter.addWidget(self._build_screenshot_pane())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setChildrenCollapsible(False)
        return splitter

    def _build_transcript_pane(self) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        heading = QLabel("Transcript")
        heading.setProperty("role", "section")
        lay.addWidget(heading)


        self.transcript_box = QPlainTextEdit()
        self.transcript_box.setUndoRedoEnabled(True)
        lay.addWidget(self.transcript_box, 1)
        return pane

    def _build_screenshot_pane(self) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        heading = QLabel("Screenshots")
        heading.setProperty("role", "section")
        head.addWidget(heading)
        head.addStretch(1)

        mode_label = QLabel("Display:")
        mode_label.setProperty("role", "caption")
        head.addWidget(mode_label)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(RENDER_MODES))
        self._mode_combo.setCurrentText(self._render_mode)
        # currentTextChanged, not activated, so programmatic changes re-render too.
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        head.addWidget(self._mode_combo)

        add_btn = QPushButton("Add screenshot…")
        add_btn.clicked.connect(self._import_screenshots)
        head.addWidget(add_btn)
        lay.addLayout(head)

        self._hint = QLabel(self._compute_hint())
        self._hint.setProperty("role", "caption")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        holder = QFrame()
        holder.setProperty("panel", "true")
        holder_lay = QVBoxLayout(holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)
        holder_lay.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(12, 12, 12, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_host)
        holder_lay.addWidget(self._scroll)

        lay.addWidget(holder, 1)
        return pane

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)
        bar.addStretch(1)

        save_btn = QPushButton("Save changes")
        save_btn.clicked.connect(self._save_clicked)
        bar.addWidget(save_btn)

        bar.addSpacing(16)

        self._gen_btn = QPushButton("Generate documentation")
        self._gen_btn.setProperty("variant", "primary")
        self._gen_btn.clicked.connect(self._save_and_generate)
        bar.addWidget(self._gen_btn)
        return bar

    # ---- Status ---- #

    def _set_status(self, text: str) -> None:
        self._status.showMessage(text)

    def _compute_hint(self) -> str:
        if not self._screenshot_events:
            return "No screenshots — narration only."
        if self._all_legacy:
            return "Blurred at capture — display mode is fixed."
        return (
            "Click a screenshot to edit it. Deleting near-duplicates "
            "improves the result."
        )

    # ---- Population / thumbnails ---- #

    def _populate(self) -> None:
        # This join separator is the one apply_transcript_edit() splits on in _save().
        self.transcript_box.setPlainText(editable_transcript(self.session_data))

        self._rebuild_thumbnails()

        count = len(self._screenshot_events)
        self._set_status(
            f"{count} screenshot{'s' if count != 1 else ''} • edit the transcript, then save or generate."
        )

    def _render_pil(self, event: dict, with_crop: bool = True):
        """Render an event at the current mode; with_crop=False is only for the crop
        tool, which must display the uncropped image."""
        try:
            return render_event_image(
                self.session_dir, event, self._render_mode, self.config, with_crop=with_crop
            )
        except Exception:
            log.exception("editor: failed to render %s", event.get("data", {}).get("filename"))
            return None

    def _rebuild_thumbnails(self) -> None:
        """Rebuild every card at the current render mode, preserving delete marks.

        Cards need setParent(None) + deleteLater(); dropping the Python reference
        does not remove a widget from its layout.
        """
        pending = set(self._deleted_ids)

        for card in self._cards.values():
            self._grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        live_ids = {e.get("event_id") for e in self.session_data.get("events", [])}
        for event in self._screenshot_events:
            if event.get("event_id") in live_ids:
                self._add_card(event)

        self._deleted_ids = {eid for eid in pending if eid in self._cards}
        for eid in self._deleted_ids:
            self._cards[eid].set_marked(True)

        self._hint.setText(self._compute_hint())
        # _all_legacy can flip when photos are imported, so re-apply it on rebuild.
        self._mode_combo.setEnabled(not self._all_legacy)
        self._update_remaining_status()

    def _add_card(self, event: dict) -> None:
        img = self._render_pil(event)
        if img is None:
            return
        pixmap = pil_to_qpixmap(img)
        if pixmap is None:
            return

        offset_s = event["timestamp_offset_ms"] / 1000
        click = event["data"].get("click_type", "left")
        click_label = {"manual": "manual capture", "imported": "imported"}.get(
            click, f"{click}-click"
        )
        caption = f"{offset_s:.1f}s • {click_label}"
        if event_note(event):
            caption += " • note"

        eid = event["event_id"]
        card = ThumbCard(
            pixmap,
            caption,
            on_open=lambda e=event: self._open_full_image(e),
            on_toggle=lambda marked, e=eid: self._on_card_toggled(e, marked),
            parent=self._grid_host,
        )
        index = len(self._cards)
        row, col = divmod(index, self.THUMBS_PER_ROW)
        self._grid.addWidget(card, row, col)
        self._cards[eid] = card

    def _on_card_toggled(self, event_id: str, marked: bool) -> None:
        if marked:
            self._deleted_ids.add(event_id)
        else:
            self._deleted_ids.discard(event_id)
        self._update_remaining_status()

    def _update_remaining_status(self) -> None:
        remaining = len(self._cards) - len(self._deleted_ids)
        self._set_status(f"{remaining} screenshot(s) will be sent for documentation.")

    # ---- Render mode ---- #

    def _on_mode_changed(self, mode: str) -> None:
        if not mode or mode == self._render_mode:
            return
        self._render_mode = mode
        self.session_data["screenshot_render_mode"] = mode
        self._rebuild_thumbnails()
        self._set_status(f"Display mode: {mode}. Save to keep it.")

    # ---- Full-size preview + redaction ---- #

    def _open_full_image(self, event: dict) -> None:
        """Open the full-size editor over the whole session so the arrow keys can
        step through it; the list is the grid's live events, so the dialog's
        position counter matches what is on screen."""
        ordered = [e for e in self._screenshot_events if e.get("event_id") in self._cards]
        try:
            index = ordered.index(event)
        except ValueError:
            ordered, index = [event], 0

        try:
            dialog = RedactionDialog(
                ordered,
                index,
                render=self._render_pil,
                render_mode=self._render_mode,
                parent=self,
            )
        except ValueError:
            QMessageBox.critical(self, "AutoDoc", "Could not open image.")
            return

        dialog.exec()
        if dialog.is_dirty():
            self._rebuild_thumbnails()
            self._set_status("Screenshot edited. Save to keep it.")

    # ---- Add Screenshot (append externally-sourced photos) ---- #

    def _import_screenshots(self) -> None:
        """Attach photos taken outside AutoDoc, via recorder/session.py's
        import_screenshots(). Files are copied to disk on selection but only enter
        session.json on Save."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add screenshots",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*.*)",
        )
        if not paths:
            return

        from recorder.session import import_screenshots

        try:
            self.session_data = import_screenshots(
                self.session_dir, self.session_data, [Path(p) for p in paths]
            )
        except OSError as exc:
            QMessageBox.critical(self, "AutoDoc", f"Could not import screenshot(s):\n{exc}")
            return

        self._screenshot_events = self._collect_screenshots()
        self._all_legacy = self._compute_all_legacy()
        self._rebuild_thumbnails()
        self._set_status(f"Added {len(paths)} screenshot(s) — click Save changes to keep them.")

    # ---- Save ---- #

    def _save_clicked(self) -> None:
        self._save()

    def _save(self) -> bool:
        """Persist edits to session.json, deleting marked screenshots and their PNGs.

        Returns True on success.
        """
        edited_text = self.transcript_box.toPlainText().strip()
        events = self.session_data.get("events", [])

        kept_events = []
        for evt in events:
            if evt.get("type") == "screenshot" and evt.get("event_id") in self._deleted_ids:
                png = self.session_dir / evt["data"]["filename"]
                try:
                    png.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                kept_events.append(evt)

        self.session_data["events"] = kept_events

        # Must run after events are reassigned; it walks session_data's segments.
        mapped = apply_transcript_edit(self.session_data, edited_text)
        log.info(
            "session_editor: transcript saved %s",
            "per-segment (timeline kept)" if mapped
            else "as a full-transcript blob (paragraph count changed)",
        )

        if not self._all_legacy:
            self.session_data["screenshot_render_mode"] = self._render_mode
        self._screenshot_events = [
            e for e in self._screenshot_events if e.get("event_id") not in self._deleted_ids
        ]

        json_path = self.session_dir / "session.json"
        tmp = json_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.session_data, f, indent=2, ensure_ascii=False)
            tmp.replace(json_path)
        except OSError as exc:
            QMessageBox.critical(self, "AutoDoc", f"Could not save session:\n{exc}")
            return False

        self._deleted_ids.clear()
        self._rebuild_thumbnails()

        remaining = len(self._cards)
        self._set_status(
            f"Changes saved. {remaining} screenshot{'s' if remaining != 1 else ''} remaining."
        )
        return True

    # ---- Generate ---- #

    def _save_and_generate(self) -> None:
        """Save, then hand off to the Sessions window's pipeline and close. Saving
        first matters: generation/run.py re-reads session.json from disk."""
        if not self._save():
            return

        self._on_generate_cb(self.session_data, self.session_dir)
        self.close()

    # ---- Lifecycle ---- #

    def closeEvent(self, event) -> None:
        """Drop the strong reference so the window can actually be freed."""
        try:
            _open_editors.remove(self)
        except ValueError:
            pass
        super().closeEvent(event)
