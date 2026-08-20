"""Sessions browser: list saved sessions, edit, generate, retranscribe, publish.

Widgets are touched only on the Qt main thread; worker threads report back
through ui_service.call_soon().
"""

import json
import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
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

# Logical pixels; Qt scales for DPI.
_MIN_W, _MIN_H = 760, 480
_DEFAULT_W, _DEFAULT_H = 980, 600
_ROW_HEIGHT = 28

# Initial widths only; every column stays user-resizable.
_COL_WIDTHS = {
    COL_NAME: 320,
    COL_DURATION: 88,
    COL_SCREENSHOTS: 104,
    COL_SEGMENTS: 152,
    COL_STATUS: 128,
}


# ---- Session discovery ---- #

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


# ---- Entry point (must be called on the Qt main thread) ---- #

def open_sessions_window(
    config: AutoDocConfig,
    get_model: Optional[Callable[[], object]] = None,
    start_resume: Optional[Callable[[Path], None]] = None,
    is_idle: Optional[Callable[[], bool]] = None,
    quit_for_update: Optional[Callable[[], None]] = None,
) -> None:
    """Open (or re-focus) the Sessions window on the Qt main thread.

    get_model returns the tray's already-loaded Whisper model, or None while it
    is still loading, so Retranscribe never loads a second copy.
    """
    global _open_window
    if _open_window is not None and _open_window.isVisible():
        _open_window.raise_()
        _open_window.activateWindow()
        return

    _open_window = SessionsWindow(config, get_model, start_resume, is_idle, quit_for_update)
    ui_service.set_main_window(_open_window)
    _open_window.show()


# ---- Window class ---- #

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
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        self._build_ui()
        self._refresh()
        self._table.setFocus()

    # ---- UI construction ---- #

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setExpanding(False)
        # app.qss draws the one seam line; Qt's own base line ignores the tokens.
        tabs.tabBar().setDrawBase(False)
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
        corner = QWidget()
        # Named so app.qss can paint its background; unstyled it renders black.
        corner.setObjectName("tabCornerActions")
        lay = QHBoxLayout(corner)
        # 4px top matches QTabBar::tab's margin-top, so these share the tabs' baseline.
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
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        root.addWidget(self._build_table(), 1)
        root.addLayout(self._build_action_bar())

        return page

    def _build_table(self) -> QWidget:
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
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.doubleClicked.connect(lambda _idx: self._edit())

        table.setItemDelegateForColumn(
            self._model.columnCount() - 1, StatusDelegate(table)
        )

        # Explicit row height; letting Qt derive it from font ascent clips descenders.
        vh = table.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(_ROW_HEIGHT)

        hh = table.horizontalHeader()
        # Sorting is not implemented, so the headers must not look clickable.
        hh.setSectionsClickable(False)
        hh.setSectionsMovable(False)
        hh.setHighlightSections(False)
        for col in range(self._model.columnCount()):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            hh.resizeSection(col, _COL_WIDTHS.get(col, 100))
        hh.setStretchLastSection(True)
        hh.setFixedHeight(_ROW_HEIGHT)

        self._table = table
        lay.addWidget(table)
        return container

    def _build_action_bar(self) -> QHBoxLayout:
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

        self._publish_btn = QPushButton("Publish to BookStack")
        self._publish_btn.clicked.connect(self._publish)
        bar.addWidget(self._publish_btn)

        bar.addSpacing(16)

        self._gen_btn = QPushButton("Generate documentation")
        self._gen_btn.setProperty("variant", "primary")
        self._gen_btn.clicked.connect(self._generate)
        bar.addWidget(self._gen_btn)
        return bar

    # ---- Data helpers ---- #

    def _set_status(self, text: str) -> None:
        self._status.showMessage(text)

    def _confirm(self, title: str, text: str) -> bool:
        """Yes/No confirmation; returns True only on an explicit Yes."""
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

    # ---- Actions ---- #

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

        open_editor_window(
            session_data, session["session_dir"], self.config, self._generate_from_editor
        )

    def _generate_from_editor(self, session_data: dict, session_dir: Path) -> None:
        """Called by the editor when the user clicks Generate inside it."""
        self._refresh()
        # This path used to skip the guard, allowing a second concurrent run.
        if self._generating:
            QMessageBox.information(self, "AutoDoc", "A generation is already in progress.")
            return
        self._run_generation(
            {
                "session_id": session_data.get("session_id", session_dir.name),
                "name": session_data.get("name") or session_dir.name,
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
            from generation.run import run_generation

            result = run_generation(
                config=self.config,
                session_dir=session["session_dir"],
            )
            primary = result.paths[0]
            publish = result.publish
            truncated = result.truncated
            ui_service.call_soon(
                lambda: self._on_generated(primary, publish, truncated)
            )
        except Exception as exc:
            log.exception("_do_generate: generation failed")
            msg = str(exc)
            ui_service.call_soon(lambda: self._on_generate_error(msg))

    # ---- BookStack publish / retry ---- #

    def _publish(self) -> None:
        """Push an already-generated session to BookStack, or retry a failed push.

        Works even when bookstack_enabled is off; the button press is the consent.
        """
        session = self._selected_session()
        if not session:
            return
        if not self.config.bookstack_enabled:
            if not self._confirm(
                "AutoDoc — Publish",
                "Automatic BookStack publishing is turned off in Settings.\n\n"
                "Publish this session anyway?",
            ):
                return

        self._publish_btn.setEnabled(False)
        self._publish_btn.setText("Publishing…")
        self._set_status(f"Publishing '{session['name']}' to BookStack…")

        session_dir: Path = session["session_dir"]
        # A copy, so forcing the flag never touches the on-disk setting.
        cfg = replace(self.config, bookstack_enabled=True)

        def _work() -> None:
            """Worker thread. Reports back via ui_service.call_soon ONLY."""
            try:
                from generation.run import publish_existing

                result = publish_existing(config=cfg, session_dir=session_dir)
                ui_service.call_soon(lambda: self._on_published(result))
            except Exception as exc:
                log.exception("_publish: publish failed")
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_published_error(msg))

        threading.Thread(target=_work, name="bookstack-publish", daemon=True).start()

    def _reset_publish_button(self) -> None:
        self._publish_btn.setEnabled(True)
        self._publish_btn.setText("Publish to BookStack")

    def _on_published(self, result) -> None:
        self._reset_publish_button()
        if result.skipped:
            self._set_status(f"Not published — {result.reason}")
            QMessageBox.information(
                self, "AutoDoc", f"Nothing was published:\n\n{result.reason}"
            )
            return
        if not result.ok:
            self._set_status("Publish failed.")
            QMessageBox.critical(
                self,
                "AutoDoc — BookStack",
                f"Could not publish to BookStack:\n\n{result.reason}",
            )
            return
        self._set_status(f"Published: {result.page_url}")
        QMessageBox.information(
            self,
            "AutoDoc — BookStack",
            f"Published {result.uploaded} screenshot(s) to:\n{result.page_url}\n\n"
            "The page is tagged Status: draft until someone reviews it.",
        )

    def _on_published_error(self, error: str) -> None:
        self._reset_publish_button()
        self._set_status("Publish failed.")
        QMessageBox.critical(
            self, "AutoDoc — BookStack", f"Could not publish to BookStack:\n\n{error}"
        )

    def _reset_gen_button(self) -> None:
        self._generating = False
        self._gen_btn.setEnabled(True)
        self._gen_btn.setText("Generate documentation")

    def _on_generated(self, output_path: Path, publish=None, truncated: bool = False) -> None:
        """Report a finished generation, including truncation and the publish outcome.

        Truncation is reported first and returns early; a truncated document is
        never published.
        """
        self._reset_gen_button()
        self._set_status(f"Saved: {output_path.name}")

        if truncated:
            self._set_status(f"Saved: {output_path.name} — INCOMPLETE")
            QMessageBox.warning(
                self,
                "AutoDoc — Document is incomplete",
                "The AI model ran out of room and stopped part-way through this "
                "document.\n\n"
                "It was still saved, and it starts with a note saying it is "
                "incomplete — but the end of the procedure is missing, so don't "
                "hand it to anyone as-is. It was NOT published to BookStack.\n\n"
                "To get a complete document: open the session, delete screenshots "
                "that don't show a distinct step, and generate again. Screenshots "
                "use up far more of the model's room than the narration does.",
            )
            if self._confirm(
                "AutoDoc — Incomplete document saved",
                f"Open the partial document anyway?\n\n{output_path}",
            ):
                self._reveal(output_path)
            return

        # A skipped publish (turned off, or no token) is not news — say nothing.
        if publish is not None and not publish.skipped and not publish.ok:
            self._set_status(f"Saved: {output_path.name} — BookStack publish FAILED")
            QMessageBox.warning(
                self,
                "AutoDoc — BookStack publish failed",
                "The documentation was generated and saved locally, but publishing "
                "to BookStack failed:\n\n"
                f"{publish.reason}\n\n"
                "Use 'Publish to BookStack' to retry — it won't regenerate the "
                "document or create a second page.",
            )
            return

        detail = ""
        if publish is not None and publish.ok:
            self._set_status(f"Saved: {output_path.name} — published to BookStack")
            detail = f"\n\nPublished to BookStack:\n{publish.page_url}"

        if self._confirm(
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}{detail}\n\nOpen the file now?",
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

    # ---- Retranscribe ---- #

    def _retranscribe(self) -> None:
        """Re-run Whisper on a session's saved audio; allowed on any session with audio."""
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

            events = [e for e in data.get("events", []) if e.get("type") != "transcript_segment"]
            events.extend(segments)
            data["events"] = sorted(events, key=lambda e: e.get("timestamp_offset_ms", 0))
            data["status"] = "ready"
            # The blob lives on the session, not on an event, so it must be popped
            # explicitly or it would override the new transcript at generation time.
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

    # ---- Record More (PRD F5) ---- #

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

    # ---- Delete / Misc ---- #

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

    # ---- Lifecycle ---- #

    def closeEvent(self, event) -> None:
        """Clear the singleton and ui_service pointer so a later open builds a fresh
        window rather than raising a deleted one."""
        global _open_window
        _open_window = None
        if ui_service.get_main_window() is self:
            ui_service.set_main_window(None)
        super().closeEvent(event)
