"""
sessions_window/sessions_window.py — Sessions browser UI (PySide6).

A window listing all saved sessions. From here the user can:
  • Edit  — opens the built-in session editor (sessions_window/session_editor.py)
  • Generate documentation — sends the session to the configured backend
  • Retranscribe — re-runs Whisper on the saved audio (recovery path for
    sessions whose transcription failed or timed out)
  • Record more / Export / Delete

THREADING MODEL (do not regress — see ui_service.py for the full story):
Widgets may only be touched from the Qt main thread. open_sessions_window()
must therefore be called on it (the tray schedules it via
ui_service.call_soon). Worker threads (generation, retranscribe) never touch
widgets: they report back via ui_service.call_soon.

STYLING: governed entirely by pyside6-ui-style-guide.md + theme/app.qss. This
module sets object names and dynamic properties; it must never call
setStyleSheet, name a color, or set a font.
"""

import json
import logging
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import ui_service
from config import AutoDocConfig, get_app_dir
from sessions_window.sessions_model import (
    COL_DURATION,
    COL_NAME,
    COL_SCREENSHOTS,
    COL_SEGMENTS,
    COL_STATUS,
    SessionsModel,
)
from sessions_window.status_delegate import StatusDelegate

log = logging.getLogger(__name__)

# Singleton: re-focus the existing window instead of stacking duplicates.
_open_window: Optional["SessionsWindow"] = None

# Fixed geometry from the style guide §6. Logical pixels — Qt scales for DPI.
_MIN_W, _MIN_H = 760, 480
_DEFAULT_W, _DEFAULT_H = 980, 600
_ROW_HEIGHT = 28

# Starting column widths. Every column is user-resizable from here (drag the
# header divider); these are only the initial values. Wide enough for each
# header's own text so nothing starts out elided.
_COL_WIDTHS = {
    COL_NAME: 320,
    COL_DURATION: 88,
    COL_SCREENSHOTS: 104,
    COL_SEGMENTS: 152,
    COL_STATUS: 128,
}


# ------------------------------------------------------------------ #
# Session discovery                                                    #
# ------------------------------------------------------------------ #

def _load_sessions(config: AutoDocConfig) -> list[dict]:
    """Scan the sessions directory and return metadata for each valid session."""
    sessions_root = get_app_dir() / config.sessions_dir
    if not sessions_root.exists():
        return []

    results = []
    for session_dir in sorted(sessions_root.iterdir(), reverse=True):
        json_path = session_dir / "session.json"
        if not json_path.exists():
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        events = data.get("events", [])
        screenshot_count = sum(1 for e in events if e.get("type") == "screenshot")
        segment_count = sum(1 for e in events if e.get("type") == "transcript_segment")

        duration = ""
        started = data.get("started_at", "")
        stopped = data.get("stopped_at", "")
        if started and stopped:
            try:
                s = datetime.fromisoformat(started)
                e = datetime.fromisoformat(stopped)
                total_s = int((e - s).total_seconds())
                duration = f"{total_s // 60}m {total_s % 60:02d}s"
            except ValueError:
                pass

        results.append(
            {
                "session_id": data.get("session_id", session_dir.name),
                "name": data.get("name") or data.get("session_id", session_dir.name),
                "session_dir": session_dir,
                "started_at": started,
                "duration": duration,
                "screenshot_count": screenshot_count,
                "segment_count": segment_count,
                "status": data.get("status", "unknown"),
            }
        )
    return results


# ------------------------------------------------------------------ #
# Entry point (must be called on the Qt main thread)                   #
# ------------------------------------------------------------------ #

def open_sessions_window(
    config: AutoDocConfig,
    get_model: Optional[Callable[[], object]] = None,
    start_resume: Optional[Callable[[Path], None]] = None,
    is_idle: Optional[Callable[[], bool]] = None,
    quit_for_update: Optional[Callable[[], None]] = None,
) -> None:
    """
    Open (or re-focus) the Sessions window.

    Args:
        config:        Loaded AutoDocConfig.
        get_model:     Callable returning the tray's preloaded Whisper model,
                       or None while it's still loading. Used by Retranscribe
                       so we never load a duplicate model into memory.
        start_resume:  Callable(session_dir) that begins append-recording into
                       an existing session (PRD F5). Owned by TrayApp — only
                       it may start recordings.
        is_idle:       Callable() -> bool, True when TrayApp's state machine
                       is IDLE. Settings tab's "Check for Updates" refuses to
                       apply an update while a recording/transcription is in
                       progress.
        quit_for_update: Callable() that cleanly shuts AutoDoc down, called by
                       the Settings tab immediately after it spawns the
                       detached updater script (see updater.py).
    """
    global _open_window
    if _open_window is not None and _open_window.isVisible():
        _open_window.raise_()
        _open_window.activateWindow()
        return

    _open_window = SessionsWindow(config, get_model, start_resume, is_idle, quit_for_update)
    ui_service.set_main_window(_open_window)
    _open_window.show()


# ------------------------------------------------------------------ #
# Window class                                                         #
# ------------------------------------------------------------------ #

class SessionsWindow(QMainWindow):
    def __init__(
        self,
        config: AutoDocConfig,
        get_model: Optional[Callable[[], object]] = None,
        start_resume: Optional[Callable[[Path], None]] = None,
        is_idle: Optional[Callable[[], bool]] = None,
        quit_for_update: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._get_model = get_model
        self._start_resume = start_resume
        self._is_idle = is_idle
        self._quit_for_update = quit_for_update
        self._generating = False
        self._retranscribing = False

        self.setWindowTitle("AutoDoc — Sessions")
        # setMinimumSize on the WINDOW is sanctioned (§6); the rule against it
        # applies to inner widgets, where it's used to force layouts instead of
        # fixing size policies.
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        self._build_ui()
        self._refresh()
        # Focus the table, not the first toolbar button. Without this, Refresh
        # (the first focusable widget) takes initial focus and renders its
        # :focus border, so it looks like a different button variant from "Open
        # output folder" sitting right next to it — same variant, one just had a
        # focus ring. The table is also the correct place for focus to start:
        # it's the primary content, and arrow keys immediately do something
        # useful.
        self._table.setFocus()

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setExpanding(False)
        # False: QTabWidget::pane's border-top is the one seam line. Qt's own
        # base line is drawn in Fusion's colour rather than a token and gets
        # interrupted by the selected tab and the corner widget, which is what
        # made it look like a partially cut-off line. See app.qss.
        tabs.tabBar().setDrawBase(False)
        # Refresh / Open output folder live in the tab bar's corner rather than
        # in their own toolbar row below it. That row cost a full 28px of
        # button plus a 12px gap — ~40px of stacked chrome above the table for
        # two rarely-used actions, which is what made the header feel heavy.
        # A corner widget shares the tab bar's existing 30px band instead, so
        # the table starts immediately under the tabs.
        tabs.setCornerWidget(self._build_corner_actions(), Qt.Corner.TopRightCorner)
        self.setCentralWidget(tabs)

        tabs.addTab(self._build_sessions_tab(), "Sessions")

        from sessions_window.settings_panel import SettingsPanel

        tabs.addTab(
            SettingsPanel(self.config, is_idle=self._is_idle, quit_for_update=self._quit_for_update),
            "Settings",
        )

        status = QStatusBar()
        self.setStatusBar(status)
        self._status = status

    def _build_corner_actions(self) -> QWidget:
        """Quiet utility actions hosted in the tab bar's top-right corner."""
        corner = QWidget()
        # Named so app.qss can paint its background. A bare QWidget in the
        # tab-bar row paints nothing, and the strip above these buttons then
        # renders as raw black — see #tabCornerActions in app.qss.
        corner.setObjectName("tabCornerActions")
        lay = QHBoxLayout(corner)
        # 4px top margin matches QTabBar::tab's margin-top in app.qss so these
        # buttons share the tabs' baseline instead of being clipped against the
        # top edge of the window's client area.
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("variant", "quiet")
        refresh_btn.clicked.connect(self._refresh)
        lay.addWidget(refresh_btn)

        output_btn = QPushButton("Open output folder")
        output_btn.setProperty("variant", "quiet")
        output_btn.clicked.connect(self._open_output_folder)
        lay.addWidget(output_btn)
        return corner

    def _build_sessions_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        # Panel-level margins (12), not window-level (16): this page sits
        # *inside* the tab pane, which is itself inside the window's chrome, so
        # 16 here stacked a second full window margin under the tab bar.
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # ── Table (the ONE stretching child) ─────────────────────── #
        root.addWidget(self._build_table(), 1)

        # ── Action bar (chrome — stretch 0) ──────────────────────── #
        root.addLayout(self._build_action_bar())

        return page

    def _build_table(self) -> QWidget:
        # The container owns the border so the view can be NoFrame — avoids the
        # doubled/partial outline you get when both draw one (§8, §9).
        container = QFrame()
        container.setProperty("panel", "true")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._model = SessionsModel(self)
        table = QTableView()
        table.setModel(self._model)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        table.setFrameShape(QFrame.NoFrame)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.ElideRight)
        # Shrink by eliding + scrolling, never by forcing the window wider (§6).
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.doubleClicked.connect(lambda _idx: self._edit())

        # Status pills.
        table.setItemDelegateForColumn(
            self._model.columnCount() - 1, StatusDelegate(table)
        )

        # Fixed 28px rows — the descender rule (§4). Explicit, never implicit:
        # letting Qt derive row height from font ascent is what clips y/g/p/Q.
        vh = table.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(_ROW_HEIGHT)

        hh = table.horizontalHeader()
        # Sorting is NOT implemented, so the headers must not look or behave
        # *clickable* — appearance and behaviour have to match (§9), otherwise
        # they read as "buttons that do nothing". Resizing is a separate
        # affordance: Interactive only adds a drag cursor on the divider, which
        # does correspond to real behaviour, so it doesn't violate that rule.
        hh.setSectionsClickable(False)
        hh.setSectionsMovable(False)
        hh.setHighlightSections(False)
        for col in range(self._model.columnCount()):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            hh.resizeSection(col, _COL_WIDTHS.get(col, 100))
        # Absorb leftover width into the final column so widening the window
        # can't leave a dead gap to the right of the last header (§6). The
        # status column is left-aligned pills, so extra room there is invisible
        # — the same slack in a numeric column would look misaligned.
        hh.setStretchLastSection(True)
        hh.setFixedHeight(_ROW_HEIGHT)

        self._table = table
        lay.addWidget(table)
        return container

    def _build_action_bar(self) -> QHBoxLayout:
        """
        §6 order, exactly: [destructive] <stretch> [secondary…] [primary]
        8px between adjacent buttons, 16px before the primary.
        """
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)

        delete_btn = QPushButton("Delete")
        delete_btn.setProperty("variant", "danger")
        delete_btn.clicked.connect(self._delete)
        bar.addWidget(delete_btn)

        bar.addStretch(1)

        self._edit_btn = QPushButton("Edit session")
        self._edit_btn.clicked.connect(self._edit)
        bar.addWidget(self._edit_btn)

        self._retrans_btn = QPushButton("Retranscribe")
        self._retrans_btn.clicked.connect(self._retranscribe)
        bar.addWidget(self._retrans_btn)

        record_more_btn = QPushButton("Record more")
        record_more_btn.clicked.connect(self._record_more)
        bar.addWidget(record_more_btn)

        export_btn = QPushButton("Export")
        export_btn.clicked.connect(self._export)
        bar.addWidget(export_btn)

        bar.addSpacing(16)

        self._gen_btn = QPushButton("Generate documentation")
        self._gen_btn.setProperty("variant", "primary")
        self._gen_btn.clicked.connect(self._generate)
        bar.addWidget(self._gen_btn)
        return bar

    # ------------------------------------------------------------------ #
    # Data helpers                                                         #
    # ------------------------------------------------------------------ #

    def _set_status(self, text: str) -> None:
        self._status.showMessage(text)

    def _confirm(self, title: str, text: str) -> bool:
        """
        Yes/No confirmation. Returns True only on an explicit Yes.

        Wrapped in one place, and fully explicit about both the button set and
        the compared enum member, rather than relying on QMessageBox.question's
        default buttons or on PySide6's forgiving `QMessageBox.Yes` shorthand.
        Destructive actions (Delete, Retranscribe, Record more) route through
        here, so an enum/API change can't silently start returning a truthy
        value and skipping the prompt.
        """
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _refresh(self) -> None:
        sessions = _load_sessions(self.config)
        self._model.set_sessions(sessions)
        count = len(sessions)
        self._set_status(f"{count} session{'s' if count != 1 else ''} found.")

    def _selected_session(self) -> Optional[dict]:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "AutoDoc", "Please select a session first.")
            return None
        return self._model.session_at(rows[0].row())

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #

    def _edit(self) -> None:
        session = self._selected_session()
        if not session:
            return

        json_path = session["session_dir"] / "session.json"
        try:
            with open(json_path, encoding="utf-8") as f:
                session_data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "AutoDoc", f"Could not open session:\n{exc}")
            return

        from sessions_window.session_editor import open_editor_window

        # Already on the Qt main thread — open directly.
        open_editor_window(
            session_data, session["session_dir"], self.config, self._generate_from_editor
        )

    def _generate_from_editor(self, session_data: dict, session_dir: Path) -> None:
        """Called by the editor when the user clicks Generate inside it."""
        # Reload the list so counts reflect any deleted screenshots.
        self._refresh()
        self._run_generation(
            {
                "session_id": session_data.get("session_id", session_dir.name),
                "name": session_data.get("name"),
                "session_dir": session_dir,
            }
        )

    def _generate(self) -> None:
        if self._generating:
            QMessageBox.information(self, "AutoDoc", "A generation is already in progress.")
            return
        session = self._selected_session()
        if not session:
            return
        self._run_generation(session)

    def _run_generation(self, session: dict) -> None:
        # No wait-cursor: generation runs in a worker thread and the rest of
        # the app stays usable. (Note: with a LOCAL model on this same machine,
        # inference can peg every CPU core — the app may still feel sluggish,
        # but that's the model server's load, not blocking in here.)
        self._generating = True
        self._gen_btn.setEnabled(False)
        self._gen_btn.setText("Generating…")
        self._set_status(
            f"Generating documentation for '{session['name']}' in the background — "
            f"you can keep using AutoDoc."
        )

        threading.Thread(
            target=self._do_generate,
            args=(session,),
            name="doc-generate",
            daemon=True,
        ).start()

    def _do_generate(self, session: dict) -> None:
        """Worker thread. Reports back via ui_service.call_soon ONLY."""
        try:
            from generation.generator import generate_documentation
            from generation.renderers import write_outputs

            session_dir: Path = session["session_dir"]
            with open(session_dir / "session.json", encoding="utf-8") as f:
                data = json.load(f)

            markdown = generate_documentation(
                config=self.config,
                session_data=data,
                session_dir=session_dir,
            )
            paths = write_outputs(
                markdown=markdown,
                session_data=data,
                session_dir=session_dir,
                config=self.config,
                output_dir=get_app_dir() / self.config.output_dir,
            )
            ui_service.call_soon(lambda: self._on_generated(paths[0]))
        except Exception as exc:
            log.exception("_do_generate: generation failed")
            msg = str(exc)
            ui_service.call_soon(lambda: self._on_generate_error(msg))

    def _reset_gen_button(self) -> None:
        self._generating = False
        self._gen_btn.setEnabled(True)
        self._gen_btn.setText("Generate documentation")

    def _on_generated(self, output_path: Path) -> None:
        self._reset_gen_button()
        self._set_status(f"Saved: {output_path.name}")
        if self._confirm(
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}\n\nOpen the file now?",
        ):
            self._reveal(output_path)

    def _on_generate_error(self, error: str) -> None:
        self._reset_gen_button()
        self._set_status("Generation failed.")
        QMessageBox.critical(
            self,
            "AutoDoc — Generation failed",
            f"Could not generate documentation.\n\nError:\n{error}",
        )

    # ------------------------------------------------------------------ #
    # Retranscribe                                                         #
    # ------------------------------------------------------------------ #

    def _retranscribe(self) -> None:
        """Re-run Whisper on a session's saved audio. Recovery path for
        sessions stuck without a transcript (status 'transcript_failed' or a
        pre-fix 'recording'), but allowed on any session with audio."""
        if self._retranscribing:
            QMessageBox.information(self, "AutoDoc", "A retranscription is already in progress.")
            return
        session = self._selected_session()
        if not session:
            return

        wav_path = session["session_dir"] / "audio_full.wav"
        if not wav_path.exists():
            QMessageBox.critical(
                self,
                "AutoDoc",
                "This session has no saved audio file (audio_full.wav), so it "
                "cannot be retranscribed.",
            )
            return

        model = self._get_model() if self._get_model else None
        if model is None:
            QMessageBox.information(
                self,
                "AutoDoc",
                "The speech model is still loading (or failed to load).\n"
                "Check autodoc.log if this persists, then try again.",
            )
            return

        warning = "This will REPLACE the session's transcript segments."
        if session["segment_count"] == 0:
            warning = "This session currently has no transcript."
        if not self._confirm(
            "AutoDoc — Retranscribe",
            f"{warning}\n\nAny manual transcript edits will also be cleared.\n"
            f"Retranscribe '{session['name']}' now?",
        ):
            return

        self._retranscribing = True
        self._retrans_btn.setEnabled(False)
        self._retrans_btn.setText("Transcribing…")
        self._set_status(f"Retranscribing '{session['name']}'…")

        threading.Thread(
            target=self._do_retranscribe,
            args=(session, model),
            name="retranscribe",
            daemon=True,
        ).start()

    def _do_retranscribe(self, session: dict, model) -> None:
        """Worker thread. Reports back via ui_service.call_soon ONLY."""
        try:
            from transcription.whisper_runner import transcribe_audio

            session_dir: Path = session["session_dir"]
            json_path = session_dir / "session.json"
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)

            segments = transcribe_audio(
                model,
                session_dir / "audio_full.wav",
                data.get("started_at"),
            )

            # Replace old transcript segments; keep everything else.
            events = [e for e in data.get("events", []) if e.get("type") != "transcript_segment"]
            events.extend(segments)
            data["events"] = sorted(events, key=lambda e: e.get("timestamp_offset_ms", 0))
            data["status"] = "ready"
            # A stale manual edit would silently override the new transcript
            # at generation time (edited_full_transcript takes precedence) —
            # remove it; the user confirmed this in the dialog above.
            data.pop("edited_full_transcript", None)

            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(json_path)

            n = len(segments)
            ui_service.call_soon(lambda: self._on_retranscribed(session["name"], n))
        except Exception as exc:
            log.exception("_do_retranscribe: failed")
            msg = str(exc)
            ui_service.call_soon(lambda: self._on_retranscribe_error(msg))

    def _reset_retrans_button(self) -> None:
        self._retranscribing = False
        self._retrans_btn.setEnabled(True)
        self._retrans_btn.setText("Retranscribe")

    def _on_retranscribed(self, name: str, segment_count: int) -> None:
        self._reset_retrans_button()
        self._refresh()
        self._set_status(f"Retranscribed '{name}': {segment_count} segments.")

    def _on_retranscribe_error(self, error: str) -> None:
        self._reset_retrans_button()
        self._set_status("Retranscription failed.")
        QMessageBox.critical(
            self,
            "AutoDoc — Retranscribe failed",
            f"Could not retranscribe this session.\n\nError:\n{error}",
        )

    # ------------------------------------------------------------------ #
    # Record More (PRD F5)                                                 #
    # ------------------------------------------------------------------ #

    def _record_more(self) -> None:
        """Append a new recording pass onto the selected session."""
        session = self._selected_session()
        if not session:
            return
        if self._start_resume is None:
            QMessageBox.information(
                self, "AutoDoc", "Recording can only be started from the main AutoDoc app."
            )
            return
        if session["status"] == "recording":
            QMessageBox.critical(
                self, "AutoDoc", "This session appears to be recording already."
            )
            return
        if not (session["session_dir"] / "audio_full.wav").exists():
            QMessageBox.critical(
                self, "AutoDoc", "This session has no saved audio file, so it cannot be resumed."
            )
            return
        if not self._confirm(
            "AutoDoc — Record more",
            f"Continue recording into '{session['name']}'?\n\n"
            "New clicks and narration will be added to this session. When you "
            "stop, the transcript is REBUILT from the full audio — any manual "
            "transcript edits will be cleared. Screenshots and their redaction "
            "settings are kept.",
        ):
            return

        self._start_resume(session["session_dir"])
        self._set_status(
            f"Recording more into '{session['name']}' — stop from the tray icon, then Refresh."
        )

    # ------------------------------------------------------------------ #
    # Export / Delete / Misc                                               #
    # ------------------------------------------------------------------ #

    def _export(self) -> None:
        """Export the selected session as a self-contained Claude-ready folder."""
        session = self._selected_session()
        if not session:
            return

        json_path = session["session_dir"] / "session.json"
        try:
            with open(json_path, encoding="utf-8") as f:
                session_data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "AutoDoc", f"Could not read session:\n{exc}")
            return

        from generation.exporter import export_session, zip_export_folder

        exports_root = get_app_dir() / "exports"
        try:
            export_dir = export_session(
                session_data, session["session_dir"], exports_root, config=self.config
            )
            zip_path = zip_export_folder(export_dir)
        except Exception as exc:
            log.exception("_export: failed")
            QMessageBox.critical(self, "AutoDoc — Export failed", f"Export error:\n{exc}")
            return

        self._set_status(f"Exported: {zip_path.name}")

        if self._confirm(
            "AutoDoc — Exported",
            f"Session exported to:\n{zip_path}\n\nOpen the folder containing the zip now?",
        ):
            self._reveal(zip_path)

    def _delete(self) -> None:
        session = self._selected_session()
        if not session:
            return
        if not self._confirm(
            "AutoDoc",
            f"Permanently delete '{session['name']}'?\n\nThis cannot be undone.",
        ):
            return
        shutil.rmtree(session["session_dir"], ignore_errors=True)
        self._refresh()

    def _open_output_folder(self) -> None:
        output_dir = get_app_dir() / self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(output_dir)])
        else:
            subprocess.Popen(["open", str(output_dir)])

    @staticmethod
    def _reveal(path: Path) -> None:
        """Show a file in the OS file manager, selected."""
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["open", str(path.parent)])

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        """
        Clear the module singleton and the ui_service main-window pointer so a
        later "View sessions" builds a fresh window instead of trying to
        raise() a deleted one, and so dialogs don't parent to a dead widget.
        """
        global _open_window
        _open_window = None
        if ui_service.get_main_window() is self:
            ui_service.set_main_window(None)
        super().closeEvent(event)
