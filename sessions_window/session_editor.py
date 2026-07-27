"""
sessions_window/session_editor.py — session review and editing window (PySide6).

Lets users:
  - Edit the transcript as plain text (left pane)
  - Preview, expand, redact, and selectively delete screenshots (right pane)
  - Choose how screenshots are rendered — raw / blurred / circled — with a
    live re-render of the thumbnails
  - Add externally-sourced photos to the session (e.g. a phone photo of a BIOS
    screen AutoDoc physically cannot screenshot) via
    recorder/session.py::import_screenshots()
  - Save changes back to session.json
  - Kick off documentation generation directly

SCREENSHOT PANEL VISIBILITY:
  The panel is ALWAYS the full thumbnail view, never a static "no screenshots"
  notice keyed off session_type == "transcript_only". A session's session_type
  only ever controlled whether recorder/session.py auto-captured clicks — it
  was never a guarantee of zero screenshots, and since photos can be imported
  after the fact, treating it as one produces wrong output. Anything needing
  "does this session have screenshots" checks the actual event list. See
  HANDOFF.md.

TRANSCRIPT STORAGE:
  The edited text is stored as "edited_full_transcript" in session.json. When
  present, the generation backend uses it in place of the individual Whisper
  segments — which is also why Retranscribe and Record More delete that key.

SCREENSHOT RENDERING:
  Screenshots are stored RAW on disk (recorder/screenshot.py). The mode chosen
  here is stored as "screenshot_render_mode" and applied at
  preview/export/generation time, so switching it is always reversible. Legacy
  sessions (pre-raw-storage) have destructively blurred PNGs and no click
  coordinates, so the mode selector is disabled for them — and that legacy
  check is recomputed on every rebuild, because importing plain photos into a
  session can flip it.

SCREENSHOT DELETION:
  Marking a thumbnail removes the event from session.json AND deletes the PNG
  from disk on Save, so it stops consuming space and tokens.

THREADING:
  Widgets only on the Qt main thread. Inline generation runs on a worker thread
  and reports back exclusively via ui_service.call_soon. See ui_service.py.

STYLING:
  Governed by pyside6-ui-style-guide.md + theme/app.qss. No setStyleSheet, no
  colors, no fonts in this file.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

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

import ui_service
from recorder.screenshot import (
    RENDER_MODES,
    event_is_legacy,
    render_event_image,
    resolve_render_mode,
)
from sessions_window.image_bridge import pil_to_qpixmap
from sessions_window.redaction_dialog import RedactionDialog
from sessions_window.thumb_card import ThumbCard

log = logging.getLogger(__name__)

_MIN_W, _MIN_H = 760, 480
_DEFAULT_W, _DEFAULT_H = 1040, 660

# Keeps a strong reference to open editors. Without this, an editor created in
# a function scope is garbage collected the moment that function returns and
# the window vanishes instantly — the single most common "my Qt window doesn't
# appear" cause.
_open_editors: list["SessionEditorWindow"] = []


def open_editor_window(
    session_data: dict,
    session_dir: Path,
    config,
    on_generate: Optional[Callable[[dict, Path], None]] = None,
) -> None:
    """
    Open the session editor. Must be called on the Qt main thread.

    Args:
        session_data:  Parsed session.json dict.
        session_dir:   Path to the session folder.
        config:        AutoDocConfig instance.
        on_generate:   Optional callback(session_data, session_dir) invoked
                       when the user clicks Generate from within the editor.
    """
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
        on_generate: Optional[Callable[[dict, Path], None]],
    ) -> None:
        super().__init__()
        self.session_data = dict(session_data)   # local mutable copy
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

    # ------------------------------------------------------------------ #
    # Session data helpers                                                 #
    # ------------------------------------------------------------------ #

    def _collect_screenshots(self) -> list[dict]:
        return sorted(
            [e for e in self.session_data.get("events", []) if e.get("type") == "screenshot"],
            key=lambda e: e["timestamp_offset_ms"],
        )

    def _compute_all_legacy(self) -> bool:
        return bool(self._screenshot_events) and all(
            event_is_legacy(e) for e in self._screenshot_events
        )

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())
        root.addWidget(self._build_splitter(), 1)   # the one stretching child
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
        # Both panes expand, transcript slightly narrower. Stretch factors, not
        # fixed widths — §6 forbids pinning inner widget sizes to force layout.
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setChildrenCollapsible(False)
        return splitter

    def _build_transcript_pane(self) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)                 # label → help text gap

        heading = QLabel("Transcript")
        heading.setProperty("role", "section")
        lay.addWidget(heading)

        hint = QLabel("Edit the narration text below. Whisper errors can be corrected here.")
        hint.setProperty("role", "caption")
        hint.setWordWrap(True)
        lay.addWidget(hint)

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
        # currentTextChanged (not activated) so a programmatic change is also
        # caught; guarded inside the handler against no-op re-selection.
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

        # Card grid inside a scroll area. The container QFrame owns the border
        # so the scroll area itself stays NoFrame (app.qss sets QScrollArea
        # border: none) — one border per level of nesting.
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
        """§6 order: [destructive] <stretch> [secondary…] [primary]."""
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

    # ------------------------------------------------------------------ #
    # Status                                                               #
    # ------------------------------------------------------------------ #

    def _set_status(self, text: str) -> None:
        self._status.showMessage(text)

    def _compute_hint(self) -> str:
        """Depends on actual screenshot state, never on session_type."""
        if not self._screenshot_events:
            return (
                "No screenshots yet — narration alone will be sent for documentation. "
                "Use “Add screenshot…” to attach photos taken outside the app "
                "(e.g. BIOS setup steps AutoDoc can't screenshot directly)."
            )
        if self._all_legacy:
            return (
                "This session predates editable screenshot effects — its images were "
                "blurred permanently at capture, so the display mode cannot be changed."
            )
        return (
            "Click a screenshot to enlarge it and pixelate sensitive areas. "
            "Tip: remove the last few stop-click frames."
        )

    # ------------------------------------------------------------------ #
    # Population / thumbnails                                              #
    # ------------------------------------------------------------------ #

    def _populate(self) -> None:
        events = self.session_data.get("events", [])

        if "edited_full_transcript" in self.session_data:
            transcript_text = self.session_data["edited_full_transcript"]
        else:
            segments = sorted(
                [e for e in events if e.get("type") == "transcript_segment"],
                key=lambda e: e["timestamp_offset_ms"],
            )
            transcript_text = "\n\n".join(
                (e.get("edited_text") or e["data"]["text"]).strip() for e in segments
            )
        self.transcript_box.setPlainText(transcript_text)

        self._rebuild_thumbnails()

        count = len(self._screenshot_events)
        self._set_status(
            f"{count} screenshot{'s' if count != 1 else ''} • edit the transcript, then save or generate."
        )

    def _render_pil(self, event: dict):
        """Render an event through the pipeline at the current mode."""
        try:
            return render_event_image(self.session_dir, event, self._render_mode, self.config)
        except Exception:
            log.exception("editor: failed to render %s", event.get("data", {}).get("filename"))
            return None

    def _rebuild_thumbnails(self) -> None:
        """
        Rebuild every card at the current render mode, preserving delete marks.

        Cards are destroyed via setParent(None) + deleteLater rather than just
        dropped from the dict — a Qt widget whose Python reference disappears
        is NOT removed from its parent layout, and the old ones would keep
        painting underneath the new grid.
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

        # Re-apply marks to cards that still exist.
        self._deleted_ids = {eid for eid in pending if eid in self._cards}
        for eid in self._deleted_ids:
            self._cards[eid].set_marked(True)

        self._hint.setText(self._compute_hint())
        # _all_legacy can flip (importing plain photos into a session that had
        # only real clicks), so the selector's enabled state is recomputed here
        # rather than only at construction.
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

    # ------------------------------------------------------------------ #
    # Render mode                                                          #
    # ------------------------------------------------------------------ #

    def _on_mode_changed(self, mode: str) -> None:
        if not mode or mode == self._render_mode:
            return
        self._render_mode = mode
        self.session_data["screenshot_render_mode"] = mode
        self._rebuild_thumbnails()
        self._set_status(
            f"Display mode set to '{mode}' — click Save changes to make it stick "
            f"for export and generation."
        )

    # ------------------------------------------------------------------ #
    # Full-size preview + redaction                                        #
    # ------------------------------------------------------------------ #

    def _open_full_image(self, event: dict) -> None:
        try:
            dialog = RedactionDialog(
                event,
                render=lambda e=event: self._render_pil(e),
                render_mode=self._render_mode,
                parent=self,
            )
        except ValueError:
            QMessageBox.critical(self, "AutoDoc", "Could not open image.")
            return

        dialog.exec()
        if dialog.is_dirty():
            self._rebuild_thumbnails()
            self._set_status("Redaction regions changed — click Save changes to keep them.")

    # ------------------------------------------------------------------ #
    # Add Screenshot (append externally-sourced photos)                    #
    # ------------------------------------------------------------------ #

    def _import_screenshots(self) -> None:
        """
        Attach photos taken outside AutoDoc to this session. Delegates the
        file-copy + event construction to recorder/session.py::
        import_screenshots() so the event schema lives in exactly one place.

        Files are copied to disk immediately on selection (consistent with how
        a live recording writes screenshots as they happen) but only become
        part of session.json on Save changes — closing without saving leaves
        untracked copies on disk, the same tradeoff as every other unsaved
        edit here.
        """
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

    # ------------------------------------------------------------------ #
    # Save                                                                 #
    # ------------------------------------------------------------------ #

    def _save_clicked(self) -> None:
        self._save()

    def _save(self) -> bool:
        """
        Persist edits to session.json. Returns True on success.
        - Removes deleted screenshot events (and their PNG files).
        - Stores the edited transcript as edited_full_transcript.
        - Stores the chosen screenshot render mode.
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
        self.session_data["edited_full_transcript"] = edited_text
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

        # Drop the now-deleted cards and re-grid so there are no visual gaps.
        self._deleted_ids.clear()
        self._rebuild_thumbnails()

        remaining = len(self._cards)
        self._set_status(
            f"Changes saved. {remaining} screenshot{'s' if remaining != 1 else ''} remaining."
        )
        return True

    # ------------------------------------------------------------------ #
    # Generate                                                             #
    # ------------------------------------------------------------------ #

    def _save_and_generate(self) -> None:
        if not self._save():
            return

        if self._on_generate_cb:
            # Delegate to the Sessions window's generation pipeline.
            self._on_generate_cb(self.session_data, self.session_dir)
            self.close()
        else:
            self._run_generation_inline()

    def _run_generation_inline(self) -> None:
        self._gen_btn.setEnabled(False)
        self._gen_btn.setText("Generating…")
        self._set_status("Generating documentation in the background — the editor stays usable.")

        def _work() -> None:
            """Worker thread. Reports back via ui_service.call_soon ONLY."""
            try:
                from config import get_app_dir
                from generation.generator import generate_documentation
                from generation.renderers import write_outputs

                markdown = generate_documentation(
                    config=self.config,
                    session_data=self.session_data,
                    session_dir=self.session_dir,
                )
                paths = write_outputs(
                    markdown=markdown,
                    session_data=self.session_data,
                    session_dir=self.session_dir,
                    config=self.config,
                    output_dir=get_app_dir() / self.config.output_dir,
                )
                ui_service.call_soon(lambda: self._on_done(paths[0]))
            except Exception as exc:
                log.exception("editor: inline generation failed")
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_error(msg))

        threading.Thread(target=_work, name="doc-generate-editor", daemon=True).start()

    def _reset_gen_button(self) -> None:
        self._gen_btn.setEnabled(True)
        self._gen_btn.setText("Generate documentation")

    def _on_done(self, output_path: Path) -> None:
        self._reset_gen_button()
        self._set_status(f"Saved: {output_path.name}")
        answer = QMessageBox.question(
            self,
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}\n\nOpen now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            import subprocess
            import sys

            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(output_path)])
            else:
                subprocess.Popen(["open", str(output_path)])

    def _on_error(self, error: str) -> None:
        self._reset_gen_button()
        self._set_status("Generation failed.")
        QMessageBox.critical(
            self, "AutoDoc — Error", f"Could not generate documentation:\n\n{error}"
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        """Drop the strong reference so the window can actually be freed."""
        try:
            _open_editors.remove(self)
        except ValueError:
            pass
        super().closeEvent(event)
