"""The Settings tab inside the Sessions window.

Mutates the AutoDocConfig instance the whole app shares, then persists with
write_config(), which rewrites config.json in full from memory.
"""

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import ui_service
from config import AutoDocConfig, write_config
from publish import tags as tag_vocab
from recorder.screenshot import RENDER_MODES
from theme import DEFAULT_THEME, THEMES, apply_theme, resolve_theme
from version import VERSION

log = logging.getLogger(__name__)

_FORMAT_ORDER = ("docx", "html", "md")
_FORMAT_LABELS = {"docx": "Word (.docx)", "html": "Web page (.html)", "md": "Markdown (.md)"}
_WHISPER_MODELS = ("tiny", "base", "small")
_THEME_LABELS = {"light": "Light", "dark": "Dark"}
_THEME_BY_LABEL = {label: name for name, label in _THEME_LABELS.items()}


class SettingsPanel(QWidget):
    def __init__(
        self,
        config: AutoDocConfig,
        is_idle: Optional[Callable[[], bool]] = None,
        quit_for_update: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_obj = config
        self._is_idle = is_idle
        self._quit_for_update = quit_for_update
        self._dirty = False
        self._pending_update = None  # updater.UpdateInfo once a check finds one
        self._build()
        self._connect_dirty_tracking()

    # ---- UI ---- #

    def _build(self) -> None:
        # setWidgetResizable(True) removes this tab's height floor, so the
        # window can still shrink.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        page = QWidget()
        scroll.setWidget(page)

        root = QVBoxLayout(page)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(24)

        root.addWidget(self._build_appearance_section())
        root.addWidget(self._build_hotkeys_section())
        root.addWidget(self._build_audio_section())
        root.addWidget(self._build_screenshots_section())
        root.addWidget(self._build_output_section())
        root.addWidget(self._build_bookstack_section())
        root.addWidget(self._build_backend_section())
        root.addWidget(self._build_updates_section())
        root.addLayout(self._build_save_bar())
        # Trailing stretch keeps the sections top-aligned in a tall window.
        root.addStretch(1)

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        """A titled panel. Returns (frame, body layout)."""
        frame = QFrame()
        frame.setProperty("panel", "true")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        heading = QLabel(title)
        heading.setProperty("role", "section")
        lay.addWidget(heading)
        return frame, lay

    @staticmethod
    def _form(grow: bool = True) -> QFormLayout:
        """A label/control form.

        grow=True is required by _field_with_help() rows, whose caption needs
        leftover space to wrap into; grow=False keeps a bare control at sizeHint.
        """
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            if grow
            else QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        return form

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "caption")
        label.setWordWrap(True)
        return label

    def _field_with_help(self, control: QWidget, help_text: str) -> QWidget:
        """Put a field's help text to the right of the control.

        The caption's horizontal policy is Ignored so it wraps into the room
        left over instead of contributing a width that widens the window.
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(control)
        caption = self._caption(help_text)
        caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(caption, 1)
        return row

    # ---- Sections --------------------------------------------------- #

    def _build_appearance_section(self) -> QFrame:
        frame, lay = self._section("Appearance")

        self._theme_combo = QComboBox()
        self._theme_combo.addItems([_THEME_LABELS[name] for name in THEMES])
        # resolve_theme() so the dropdown agrees with what is on screen, not
        # with an out-of-vocabulary config value nothing honoured.
        active = resolve_theme(getattr(self.config_obj, "theme", DEFAULT_THEME))
        self._theme_combo.setCurrentText(_THEME_LABELS[active])
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)

        # grow=False: the caption below spans the panel, so the combo must not
        # stretch across the field column.
        form = self._form(grow=False)
        form.addRow("Theme:", self._theme_combo)
        lay.addLayout(form)
        lay.addWidget(self._caption("Applies now; Save to keep it."))
        return frame

    def _on_theme_changed(self, label: str) -> None:
        """Repaint the whole app in the chosen theme.

        Deferred via ui_service.call_soon() because apply_theme() re-polishes
        every live widget, this combo included, mid-signal.
        """
        theme = _THEME_BY_LABEL.get(label)
        if theme is None:
            return
        ui_service.call_soon(lambda: self._apply_theme_now(theme))

    def _apply_theme_now(self, theme: str) -> None:
        app = QApplication.instance()
        if app is None:
            return
        try:
            apply_theme(app, theme)
        except Exception:
            # A cosmetic failure must not take out the Settings tab.
            log.exception("theme switch to %r failed", theme)
            self._status.setText(
                f"Could not switch to the {theme} theme — see autodoc.log."
            )

    def _build_hotkeys_section(self) -> QFrame:
        frame, lay = self._section("Keyboard shortcuts (while recording)")
        cfg = self.config_obj

        self._stop_edit = QLineEdit(cfg.stop_hotkey)
        self._pause_edit = QLineEdit(cfg.pause_hotkey)
        self._manual_edit = QLineEdit(cfg.manual_capture_hotkey)

        form = self._form()
        form.addRow("Stop recording:", self._field_with_help(self._stop_edit, ""))
        form.addRow("Pause / resume:", self._field_with_help(self._pause_edit, ""))
        form.addRow(
            "Screenshot without clicking:",
            self._field_with_help(self._manual_edit, ""),
        )
        lay.addLayout(form)

        lay.addWidget(self._caption(
            "Blank disables. Applies to the next recording."
        ))
        return frame

    def _build_audio_section(self) -> QFrame:
        """Which microphone recordings come from.

        Device NAMES are stored, not PortAudio indices (see
        recorder/audio_capture.py); the list is refreshable on demand.
        """
        frame, lay = self._section("Microphone")

        self._audio_combo = QComboBox()
        self._audio_combo.setObjectName("settingsAudioDevice")
        self._populate_audio_devices()

        self._audio_refresh_btn = QPushButton("Refresh")
        self._audio_refresh_btn.clicked.connect(self._refresh_audio_devices)

        device_row = QWidget()
        device_lay = QHBoxLayout(device_row)
        device_lay.setContentsMargins(0, 0, 0, 0)
        device_lay.setSpacing(8)
        device_lay.addWidget(self._audio_combo, 1)
        device_lay.addWidget(self._audio_refresh_btn)

        form = self._form()
        form.addRow(
            "Input device:",
            self._field_with_help(device_row, ""),
        )
        lay.addLayout(form)

        lay.addWidget(self._caption("Applies to the next recording."))
        return frame

    def _populate_audio_devices(self) -> None:
        """Fill the dropdown, keeping the saved device selectable while unattached."""
        from recorder.audio_capture import list_input_devices

        current = (getattr(self.config_obj, "audio_device", "") or "").strip()
        # Repopulating fires currentIndexChanged, which would flag the panel
        # dirty with nothing changed.
        self._audio_combo.blockSignals(True)
        self._audio_combo.clear()
        self._audio_combo.addItem("System default", "")

        names = []
        for dev in list_input_devices():
            self._audio_combo.addItem(dev["name"], dev["name"])
            names.append(dev["name"])
        if current and current not in names:
            self._audio_combo.addItem(f"{current} (not connected)", current)

        index = self._audio_combo.findData(current)
        self._audio_combo.setCurrentIndex(index if index >= 0 else 0)
        self._audio_combo.blockSignals(False)

    def _refresh_audio_devices(self) -> None:
        self._populate_audio_devices()
        count = max(self._audio_combo.count() - 1, 0)
        self._status.setText(f"Microphone list refreshed — {count} input device(s) found.")

    def _build_screenshots_section(self) -> QFrame:
        frame, lay = self._section("Screenshots")

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(RENDER_MODES))
        self._mode_combo.setCurrentText(self.config_obj.screenshot_mode)

        # grow=False: the help text below spans the panel, so the combo must not
        # stretch across the field column.
        form = self._form(grow=False)
        form.addRow("Default display mode:", self._mode_combo)
        lay.addLayout(form)
        lay.addWidget(self._caption(
            "blurred = hide all but the click; circled = mark it."
        ))
        return frame

    def _build_output_section(self) -> QFrame:
        frame, lay = self._section("Documentation output")

        self._format_boxes: dict[str, QCheckBox] = {}
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        current = self.config_obj.output_formats or []
        for fmt in _FORMAT_ORDER:
            box = QCheckBox(_FORMAT_LABELS[fmt])
            box.setChecked(fmt in current)
            self._format_boxes[fmt] = box
            row.addWidget(box)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(self._caption("At least one required."))
        return frame

    def _build_bookstack_section(self) -> QFrame:
        """BookStack publishing.

        The bot token is not a config key, so it is reported here rather than
        edited; base_url and timeout stay config.json-only.
        """
        frame, lay = self._section("Publish to BookStack")
        cfg = self.config_obj

        self._bs_enabled = QCheckBox(
            "Publish to BookStack automatically when documentation is generated"
        )
        self._bs_enabled.setChecked(bool(getattr(cfg, "bookstack_enabled", False)))
        self._bs_enabled.toggled.connect(self._sync_bookstack_enabled)
        lay.addWidget(self._bs_enabled)

        self._bs_book = QLineEdit(getattr(cfg, "bookstack_book", "AutoDoc"))

        def _vocab_combo(values: tuple, current: str) -> QComboBox:
            combo = QComboBox()
            combo.addItems(list(values))
            # An out-of-vocabulary config value would otherwise show as the
            # first entry.
            combo.setCurrentText(current if current in values else values[0])
            return combo

        self._bs_doc_type = _vocab_combo(
            tag_vocab.DOC_TYPE, getattr(cfg, "bookstack_tag_doc_type", "sop")
        )
        self._bs_owner = _vocab_combo(
            tag_vocab.OWNER, getattr(cfg, "bookstack_tag_owner", "it-ops")
        )
        self._bs_audience = _vocab_combo(
            tag_vocab.AUDIENCE, getattr(cfg, "bookstack_tag_audience", "all-staff")
        )
        self._bs_review = _vocab_combo(
            tag_vocab.REVIEW_CYCLE, getattr(cfg, "bookstack_tag_review_cycle", "ad-hoc")
        )

        form = self._form()
        form.addRow(
            "Book:",
            self._field_with_help(self._bs_book, "Created if it doesn't exist."),
        )
        form.addRow(
            "Doc type:",
            self._field_with_help(
                self._bs_doc_type, "Pages are always tagged Status: draft."
            ),
        )
        form.addRow(
            "Owner:",
            self._field_with_help(self._bs_owner, ""),
        )
        form.addRow(
            "Audience:",
            self._field_with_help(self._bs_audience, ""),
        )
        form.addRow(
            "Review cycle:",
            self._field_with_help(self._bs_review, ""),
        )
        lay.addLayout(form)

        self._bs_token_status = self._caption(self._token_status_text())
        lay.addWidget(self._bs_token_status)

        self._bookstack_fields = (
            self._bs_book,
            self._bs_doc_type,
            self._bs_owner,
            self._bs_audience,
            self._bs_review,
        )
        self._sync_bookstack_enabled(self._bs_enabled.isChecked())
        return frame

    @staticmethod
    def _token_status_text() -> str:
        """Publishing is a silent no-op without a token, so its status is surfaced."""
        from publish.token import load_token, token_path

        if load_token() is not None:
            return f"Bot token: found ({token_path().name}). Pages publish as the bot user."
        return (
            f"Bot token: NOT found or unreadable at {token_path()} — "
            "publishing will do nothing. See autodoc.log for the exact reason."
        )

    def _sync_bookstack_enabled(self, on: bool) -> None:
        for widget in getattr(self, "_bookstack_fields", ()):
            widget.setEnabled(on)

    def _build_backend_section(self) -> QFrame:
        frame, lay = self._section("AI server")
        cfg = self.config_obj

        self._endpoint_edit = QLineEdit(cfg.local_endpoint)
        # Widths for these two live in app.qss (#settingsEndpoint / #settingsModel).
        self._endpoint_edit.setObjectName("settingsEndpoint")

        # Editable because a model id must match the server exactly: llama.cpp
        # derives ids from GGUF filenames, Ollama uses registry tags.
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("settingsModel")
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._model_combo.setCurrentText(cfg.local_model)

        self._fetch_btn = QPushButton("Fetch")
        self._fetch_btn.clicked.connect(self._fetch_models)
        model_row = QWidget()
        model_lay = QHBoxLayout(model_row)
        model_lay.setContentsMargins(0, 0, 0, 0)
        model_lay.setSpacing(8)
        model_lay.addWidget(self._model_combo, 1)
        model_lay.addWidget(self._fetch_btn)

        self._whisper_combo = QComboBox()
        self._whisper_combo.addItems(list(_WHISPER_MODELS))
        self._whisper_combo.setCurrentText(cfg.whisper_model)

        form = self._form()
        form.addRow(
            "Endpoint:",
            self._field_with_help(self._endpoint_edit, "The AI chat endpoint."),
        )
        form.addRow(
            "Model:",
            self._field_with_help(
                model_row,
                "Click fetch to see available server models. Requires compatible endpoint.",
            ),
        )
        form.addRow(
            "Speech model:",
            self._field_with_help(
                self._whisper_combo, "Bigger is slower. Restart to apply."
            ),
        )
        lay.addLayout(form)

        # Stays invisible unless the endpoint is actually misconfigured.
        self._endpoint_warning = QLabel("")
        self._endpoint_warning.setProperty("role", "danger")
        self._endpoint_warning.setWordWrap(True)
        lay.addWidget(self._endpoint_warning)
        self._sync_endpoint_warning()
        return frame

    def _sync_endpoint_warning(self) -> None:
        """Show endpoint_warnings() — currently that port 8081 answers but bypasses
        metering, so the endpoint must be 8080 — or nothing. The backend import is
        guarded so a warning can never stop the tab building."""
        try:
            from generation.backends.local_vision_backend import endpoint_warnings

            text = " ".join(endpoint_warnings(self._endpoint_edit.text()))
        except Exception:
            log.exception("settings: endpoint warning check failed")
            text = ""
        self._endpoint_warning.setText(text)
        self._endpoint_warning.setVisible(bool(text))

    def _build_updates_section(self) -> QFrame:
        frame, lay = self._section("Updates")

        lay.addWidget(QLabel(f"Current version: {VERSION}"))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._update_btn = QPushButton("Check for updates")
        self._update_btn.clicked.connect(self._check_for_updates)
        row.addWidget(self._update_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self._update_status = self._caption("")
        lay.addWidget(self._update_status)
        return frame

    def _build_save_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)

        self._status = self._caption("")
        bar.addWidget(self._status)
        bar.addStretch(1)

        save_btn = QPushButton("Save settings")
        save_btn.setProperty("variant", "primary")
        save_btn.clicked.connect(self._save)
        bar.addWidget(save_btn)
        return bar

    # ---- Dirty tracking ---- #

    def _connect_dirty_tracking(self) -> None:
        for edit in (
            self._stop_edit,
            self._pause_edit,
            self._manual_edit,
            self._endpoint_edit,
            self._bs_book,
        ):
            edit.textEdited.connect(self._mark_dirty)
        for combo in (
            # The theme applies live but is not persisted until Save, so it is
            # still an unsaved change.
            self._theme_combo,
            self._mode_combo,
            self._whisper_combo,
            self._bs_doc_type,
            self._bs_owner,
            self._bs_audience,
            self._bs_review,
        ):
            combo.currentTextChanged.connect(self._mark_dirty)
        # Editable combo: catch both typing and picking from the list.
        self._model_combo.currentTextChanged.connect(self._mark_dirty)
        # By index, not text: the stored value is the item's data, not its label.
        self._audio_combo.currentIndexChanged.connect(self._mark_dirty)
        for box in (*self._format_boxes.values(), self._bs_enabled):
            box.toggled.connect(self._mark_dirty)

    def _mark_dirty(self, *_args) -> None:
        self._dirty = True
        self._status.setText("Unsaved changes — click Save settings to apply.")

    # ---- Model list fetch ---- #

    def _fetch_models(self) -> None:
        """Query /v1/models on a worker thread and fill the dropdown; warns about
        text-only models, which silently ignore every screenshot."""
        endpoint = self._endpoint_edit.text().strip()
        if not endpoint:
            self._status.setText("Enter the endpoint first, then Fetch.")
            return
        # Read on the UI thread and passed in; the worker never touches
        # self.config_obj.
        api_key = getattr(self.config_obj, "local_api_key", "")
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setText("…")
        self._status.setText("Querying the AI server…")

        def _work() -> None:
            try:
                from generation.backends.local_vision_backend import list_models

                models = list_models(endpoint, api_key=api_key)
                ui_service.call_soon(lambda: self._on_models(models))
            except Exception as exc:
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_models_error(msg))

        threading.Thread(target=_work, name="model-list-fetch", daemon=True).start()

    def _reset_fetch_button(self) -> None:
        self._fetch_btn.setEnabled(True)
        self._fetch_btn.setText("Fetch")

    def _on_models(self, models: list) -> None:
        self._reset_fetch_button()
        if not models:
            self._status.setText("The server responded but listed no models.")
            return
        ids = [m["id"] for m in models]
        # Repopulating the list must not clobber what the user already typed.
        typed = self._model_combo.currentText()
        self._model_combo.clear()
        self._model_combo.addItems(ids)
        self._model_combo.setCurrentText(typed or ids[0])

        text_only = [m["id"] for m in models if m["text_only"]]
        msg = f"{len(ids)} model(s) found — pick one from the dropdown."
        if text_only:
            msg += f" WARNING: text-only (cannot see screenshots): {', '.join(text_only)}"
        self._status.setText(msg)

    def _on_models_error(self, error: str) -> None:
        self._reset_fetch_button()
        self._status.setText(f"Model lookup failed — {error}")

    # ---- Save ---- #

    def _validate_hotkeys(self) -> list[str]:
        """Return error strings for invalid/duplicate hotkey combos."""
        errors: list[str] = []
        combos = {
            "Stop recording": self._stop_edit.text().strip(),
            "Pause / resume": self._pause_edit.text().strip(),
            "Manual screenshot": self._manual_edit.text().strip(),
        }
        try:
            from pynput import keyboard

            parser = keyboard.HotKey.parse
        except Exception:
            parser = None  # pynput unavailable here — skip syntax validation

        seen: dict[str, str] = {}
        for label, combo in combos.items():
            if not combo:
                continue
            if parser is not None:
                try:
                    parser(combo)
                except (ValueError, KeyError) as exc:
                    errors.append(f"{label}: invalid shortcut '{combo}' ({exc})")
                    continue
            if combo in seen:
                errors.append(f"{label} and {seen[combo]} use the same shortcut '{combo}'")
            seen[combo] = label
        return errors

    def _save(self) -> None:
        errors = self._validate_hotkeys()

        formats = [f for f in _FORMAT_ORDER if self._format_boxes[f].isChecked()]
        if not formats:
            errors.append("Select at least one documentation output format.")

        endpoint = self._endpoint_edit.text().strip()
        if not endpoint:
            errors.append("The AI server endpoint cannot be blank.")

        bs_enabled = self._bs_enabled.isChecked()
        bs_book = self._bs_book.text().strip()
        if bs_enabled and not bs_book:
            errors.append("BookStack publishing needs a book name.")

        if errors:
            self._status.setText("Not saved — " + " • ".join(errors))
            return

        cfg = self.config_obj
        # Already applied live by _on_theme_changed; this is only the persist half.
        cfg.theme = _THEME_BY_LABEL.get(self._theme_combo.currentText(), cfg.theme)
        cfg.stop_hotkey = self._stop_edit.text().strip()
        cfg.pause_hotkey = self._pause_edit.text().strip()
        cfg.manual_capture_hotkey = self._manual_edit.text().strip()
        cfg.audio_device = self._audio_combo.currentData() or ""
        cfg.screenshot_mode = self._mode_combo.currentText()
        cfg.output_formats = formats
        cfg.local_endpoint = endpoint
        cfg.local_model = self._model_combo.currentText().strip()
        cfg.bookstack_enabled = bs_enabled
        cfg.bookstack_book = bs_book
        cfg.bookstack_tag_doc_type = self._bs_doc_type.currentText()
        cfg.bookstack_tag_owner = self._bs_owner.currentText()
        cfg.bookstack_tag_audience = self._bs_audience.currentText()
        cfg.bookstack_tag_review_cycle = self._bs_review.currentText()
        restart_needed = self._whisper_combo.currentText() != cfg.whisper_model
        cfg.whisper_model = self._whisper_combo.currentText()

        try:
            write_config(cfg)
        except Exception as exc:
            log.exception("settings save failed")
            self._status.setText(f"Could not write config.json: {exc}")
            return

        msg = "Settings saved."
        if restart_needed:
            msg += " Restart AutoDoc to load the new speech model."

        # The endpoint may have just changed; recompute rather than leave the
        # warning describing the previous value.
        self._sync_endpoint_warning()

        # The token may have been dropped in while this window was open.
        from publish.token import load_token

        self._bs_token_status.setText(self._token_status_text())
        if bs_enabled and load_token() is None:
            msg += (
                " WARNING: publishing is on but no BookStack token was found, "
                "so nothing will be published."
            )
        self._dirty = False
        self._status.setText(msg)
        log.info("settings saved via Settings tab")

    # ---- Updates — manual only, via GitHub Releases (see updater.py) ---- #

    def _check_for_updates(self) -> None:
        self._update_btn.setEnabled(False)
        self._update_btn.setText("Checking…")
        self._update_status.setText("Checking GitHub for a newer release…")

        def _work() -> None:
            import updater

            try:
                info = updater.check_for_update()
                ui_service.call_soon(lambda: self._on_check_result(info))
            except updater.UpdateCheckError as exc:
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_check_error(msg))

        threading.Thread(target=_work, name="update-check", daemon=True).start()

    def _reset_update_button(self) -> None:
        self._update_btn.setEnabled(True)
        self._update_btn.setText("Check for updates")

    def _on_check_result(self, info) -> None:
        self._reset_update_button()
        if info is None:
            self._update_status.setText(f"You're on the latest version ({VERSION}).")
            return
        self._pending_update = info
        self._update_status.setText(
            f"Version {info.version} is available (current: {VERSION})."
        )
        self._show_update_dialog(info)

    def _on_check_error(self, error: str) -> None:
        self._reset_update_button()
        self._update_status.setText(error)

    def _show_update_dialog(self, info) -> None:
        """Release-notes dialog; the GitHub release body is the changelog shown."""
        dialog = QDialog(self.window())
        dialog.setWindowTitle("AutoDoc — Update available")
        dialog.setMinimumSize(480, 360)

        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        size_mb = info.asset_size / (1024 * 1024) if info.asset_size else 0
        header_text = f"Version {info.version} is available (you have {VERSION})."
        if size_mb:
            header_text += f" ~{size_mb:.0f} MB download."
        header = QLabel(header_text)
        header.setWordWrap(True)
        header.setProperty("role", "section")
        lay.addWidget(header)

        notes = QPlainTextEdit()
        notes.setPlainText(info.notes)
        notes.setReadOnly(True)
        lay.addWidget(notes, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 16, 0, 0)
        bar.setSpacing(8)
        bar.addStretch(1)
        not_now = QPushButton("Not now")
        not_now.clicked.connect(dialog.reject)
        bar.addWidget(not_now)
        bar.addSpacing(16)
        update_now = QPushButton("Update now")
        update_now.setProperty("variant", "primary")
        update_now.clicked.connect(lambda: self._start_update(dialog, info))
        bar.addWidget(update_now)
        lay.addLayout(bar)

        dialog.exec()

    def _start_update(self, dialog: QDialog, info) -> None:
        if self._is_idle is not None and not self._is_idle():
            QMessageBox.critical(
                dialog,
                "AutoDoc — Update",
                "Finish or stop the current recording before updating.\n\n"
                "Applying an update requires closing AutoDoc, which would "
                "interrupt a recording in progress.",
            )
            return

        dialog.accept()
        self._update_btn.setEnabled(False)
        self._update_btn.setText("Downloading…")
        self._update_status.setText(f"Downloading version {info.version}…")

        def _work() -> None:
            import updater

            try:
                zip_path = updater.download_update(info)
                ui_service.call_soon(lambda: self._on_downloaded(zip_path))
            except updater.UpdateCheckError as exc:
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_check_error(msg))

        threading.Thread(target=_work, name="update-download", daemon=True).start()

    def _on_downloaded(self, zip_path) -> None:
        """Spawn the detached updater script, then quit immediately; the file swap
        cannot happen in-process (see updater.py). The script does the relaunch."""
        import updater

        self._update_status.setText("Restarting to apply the update…")
        updater.launch_update_and_exit(zip_path)
        if self._quit_for_update is not None:
            self._quit_for_update()
