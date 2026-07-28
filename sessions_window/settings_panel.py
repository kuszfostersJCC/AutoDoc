"""
sessions_window/settings_panel.py — the Settings tab inside the Sessions window.

An interactive editor for the config.json values that plausibly change
semi-regularly: recording hotkeys, the default screenshot display mode,
documentation output formats, the AI backend, and the Whisper model.
Deliberately NOT exposed here: api_key (sensitive — config.json only),
directory paths and capture limits (IT-install decisions, not user settings).

PERSISTENCE MODEL: this panel mutates the SAME AutoDocConfig instance the whole
app holds (TrayApp, Session, generation all share it), then persists via
config.write_config(). Consequences, stated per field in the UI:
  - screenshot mode / output formats / backend: take effect immediately,
  - hotkeys: read when a recording starts → apply to the NEXT recording,
  - whisper model: loaded once at startup → requires an app restart.

Save writes the FULL config back to config.json. If someone hand-edits the
deployed config.json while AutoDoc is open and then presses Save here, the
hand-edit is silently overwritten by whatever the app held in memory. That's a
documented invariant (HANDOFF.md), not a bug to "fix" by merging on save —
merging would make the in-memory config and the file disagree in a way nothing
else in the app expects.

LAYOUT: every label/control pair goes through QFormLayout so the alignment is
structural rather than a per-row decision — hand-assembling a label and field
in a QHBoxLayout is how the old UI ended up with a combo box wedged against
the word "Display:". Help text sits under its field as a caption row.

STYLING: governed by pyside6-ui-style-guide.md + theme/app.qss. No
setStyleSheet, no colors, no fonts in this file.
"""

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
from version import VERSION

log = logging.getLogger(__name__)

_FORMAT_ORDER = ("docx", "html", "md")
_FORMAT_LABELS = {"docx": "Word (.docx)", "html": "Web page (.html)", "md": "Markdown (.md)"}
_WHISPER_MODELS = ("tiny", "base", "small")
_PROVIDERS = ("anthropic", "local")


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

    # ------------------------------------------------------------------ #
    # UI                                                                   #
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        # QScrollArea with setWidgetResizable(True) is what removes the height
        # floor (§6) — without it this tab imposes a minimum height on the
        # whole window and the Sessions window can't shrink to 760×480.
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
        root.setSpacing(24)          # gap between sections

        root.addWidget(self._build_hotkeys_section())
        root.addWidget(self._build_screenshots_section())
        root.addWidget(self._build_output_section())
        root.addWidget(self._build_bookstack_section())
        root.addWidget(self._build_backend_section())
        root.addWidget(self._build_updates_section())
        root.addLayout(self._build_save_bar())
        # The one stretching child is the trailing space, so sections stay
        # top-aligned instead of spreading down a tall window.
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
        """
        A label/control form. `grow` controls the field column's width policy:

        - grow=True (default) — fields expand to fill the panel. Required for
          rows built by _field_with_help(), because the caption beside the
          control needs leftover space to wrap into; with the fields pinned to
          sizeHint the row collapses to the control's width and the help text
          is squeezed to nothing. The control itself is added at stretch 0
          inside that row, so only the caption takes the extra space.
        - grow=False — fields stay at their sizeHint. For rows holding a bare
          control with no help beside it, where expanding would stretch a
          combo box across the whole panel for no reason.
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
        """
        Put a field's help text to the RIGHT of the control, not underneath.

        Captions on their own form row stack two rows per setting, which left a
        column of whitespace beside every short control. Sitting them beside
        the field uses that space instead.

        The caption must never widen the form: wordWrap(True) plus
        QSizePolicy(Ignored, Minimum) means it wraps into whatever room is left
        rather than reporting a long sizeHint and pushing the window wider (§6).
        Ignored rather than Preferred on the horizontal axis specifically
        because a Preferred caption still contributes its wrapped width, which
        is enough to stop the Sessions window shrinking to 760px.
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

    def _build_hotkeys_section(self) -> QFrame:
        frame, lay = self._section("Keyboard shortcuts (while recording)")
        cfg = self.config_obj

        self._stop_edit = QLineEdit(cfg.stop_hotkey)
        self._pause_edit = QLineEdit(cfg.pause_hotkey)
        self._manual_edit = QLineEdit(cfg.manual_capture_hotkey)

        form = self._form()
        form.addRow(
            "Stop recording:",
            self._field_with_help(
                self._stop_edit, "Ends the recording without any tray clicks being captured."
            ),
        )
        form.addRow(
            "Pause / resume:",
            self._field_with_help(
                self._pause_edit, "Suspends clicks and narration until pressed again."
            ),
        )
        form.addRow(
            "Manual screenshot:",
            self._field_with_help(
                self._manual_edit, "Captures the screen without clicking (result pages, open menus)."
            ),
        )
        lay.addLayout(form)

        lay.addWidget(self._caption(
            "Format: <ctrl>+<shift>+s — leave blank to disable. "
            "Changes apply to the next recording."
        ))
        return frame

    def _build_screenshots_section(self) -> QFrame:
        frame, lay = self._section("Screenshots")

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(RENDER_MODES))
        self._mode_combo.setCurrentText(self.config_obj.screenshot_mode)

        # grow=False: this row's help text stays full-width beneath the form
        # (it's a long sentence that reads better spanning the panel), so the
        # combo must not stretch to fill the field column.
        form = self._form(grow=False)
        form.addRow("Default display mode:", self._mode_combo)
        lay.addLayout(form)
        lay.addWidget(self._caption(
            "raw = untouched • blurred = privacy blur • circled = red circle at click. "
            "Each session can override this in the editor."
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

        lay.addWidget(self._caption(
            "Formats written when generating documentation. At least one required."
        ))
        return frame

    def _build_bookstack_section(self) -> QFrame:
        """
        BookStack publishing (PRD-v1.3-bookstack.md).

        The bot TOKEN is deliberately absent — it isn't a config key at all (it
        would be wiped by the full-config rewrite on Save) and there's no masked
        input anywhere in this app, matching the api_key rule in this module's
        docstring. Instead the panel REPORTS token status, because the failure
        mode without it is silent: publishing simply never happens, and the only
        evidence is a line in autodoc.log.

        base_url and timeout stay config.json-only. Nobody changes them, and
        hiding them would need a collapsible-section pattern the style guide
        doesn't have — not worth inventing one for two fields.
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
            # An out-of-vocabulary config value would otherwise silently show as
            # the first entry, i.e. the UI would lie about what is configured.
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
            self._field_with_help(
                self._bs_book,
                "Holding book for generated pages. Created if it doesn't exist.",
            ),
        )
        form.addRow(
            "Doc type:",
            self._field_with_help(
                self._bs_doc_type, "Doc-Type tag. Pages are always tagged Status: draft."
            ),
        )
        form.addRow(
            "Owner:",
            self._field_with_help(self._bs_owner, "Department responsible for the page."),
        )
        form.addRow(
            "Audience:",
            self._field_with_help(self._bs_audience, "Who the documentation is for."),
        )
        form.addRow(
            "Review cycle:",
            self._field_with_help(self._bs_review, "How often the page should be rechecked."),
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
        """
        Report whether the bot token is present and parseable.

        Worth surfacing in the UI rather than leaving to the log: a missing token
        makes publishing a no-op with no user-visible symptom at all, and "I
        ticked the box and nothing happened" is otherwise unfalsifiable from the
        Settings tab.
        """
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
        frame, lay = self._section("AI backend")
        cfg = self.config_obj

        self._provider_combo = QComboBox()
        self._provider_combo.addItems(list(_PROVIDERS))
        self._provider_combo.setCurrentText(cfg.provider)

        self._endpoint_edit = QLineEdit(cfg.local_endpoint)
        # Widths for these two live in app.qss (#settingsEndpoint /
        # #settingsModel) — both hold long values that truncate at the default
        # sizeHint.
        self._endpoint_edit.setObjectName("settingsEndpoint")

        # Editable combo: type a model id, or Fetch to list what the server
        # actually serves (/v1/models). Ids must match EXACTLY — llama.cpp
        # derives them from GGUF filenames, Ollama uses registry tags — which
        # is why this one is editable and the others are not.
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
            "Provider:",
            self._field_with_help(
                self._provider_combo,
                "anthropic = Claude API (needs api_key in config.json) • "
                "local = your own vision model.",
            ),
        )
        form.addRow(
            "Local endpoint:",
            self._field_with_help(
                self._endpoint_edit,
                "OpenAI-compatible chat endpoint (Ollama, LM Studio…). "
                "Used when provider = local.",
            ),
        )
        form.addRow(
            "Local model:",
            self._field_with_help(
                model_row,
                "Must match a model id on the endpoint. "
                "Fetch queries the server's /v1/models.",
            ),
        )
        form.addRow(
            "Speech model:",
            self._field_with_help(
                self._whisper_combo,
                "Whisper size — bigger is more accurate but slower. "
                "Takes effect after restarting AutoDoc.",
            ),
        )
        lay.addLayout(form)
        return frame

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

    # ------------------------------------------------------------------ #
    # Dirty tracking                                                       #
    # ------------------------------------------------------------------ #

    def _connect_dirty_tracking(self) -> None:
        """
        Flag unsaved edits — "did I forget to save?" should never be a mystery
        (field report 2026-07-14: a user unchecked a format, still got the file,
        and couldn't tell whether Save had happened).
        """
        for edit in (
            self._stop_edit,
            self._pause_edit,
            self._manual_edit,
            self._endpoint_edit,
            self._bs_book,
        ):
            edit.textEdited.connect(self._mark_dirty)
        for combo in (
            self._mode_combo,
            self._provider_combo,
            self._whisper_combo,
            self._bs_doc_type,
            self._bs_owner,
            self._bs_audience,
            self._bs_review,
        ):
            combo.currentTextChanged.connect(self._mark_dirty)
        # Editable combo: catch both typing and picking from the list.
        self._model_combo.currentTextChanged.connect(self._mark_dirty)
        for box in (*self._format_boxes.values(), self._bs_enabled):
            box.toggled.connect(self._mark_dirty)

    def _mark_dirty(self, *_args) -> None:
        self._dirty = True
        self._status.setText("Unsaved changes — click Save settings to apply.")

    # ------------------------------------------------------------------ #
    # Model list fetch                                                     #
    # ------------------------------------------------------------------ #

    def _fetch_models(self) -> None:
        """Query the local endpoint's /v1/models on a worker thread and fill the
        dropdown. Also warns about text-only models — a model without vision
        support silently ignores every screenshot."""
        endpoint = self._endpoint_edit.text().strip()
        if not endpoint:
            self._status.setText("Enter the local endpoint first, then Fetch.")
            return
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setText("…")
        self._status.setText("Querying the model server…")

        def _work() -> None:
            try:
                from generation.backends.local_vision_backend import list_models

                models = list_models(endpoint)
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

    # ------------------------------------------------------------------ #
    # Save                                                                 #
    # ------------------------------------------------------------------ #

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

        provider = self._provider_combo.currentText().strip().lower()
        endpoint = self._endpoint_edit.text().strip()
        if provider == "local" and not endpoint:
            errors.append("Provider 'local' needs a local endpoint URL.")

        bs_enabled = self._bs_enabled.isChecked()
        bs_book = self._bs_book.text().strip()
        if bs_enabled and not bs_book:
            errors.append("BookStack publishing needs a book name.")

        if errors:
            self._status.setText("Not saved — " + " • ".join(errors))
            return

        cfg = self.config_obj
        cfg.stop_hotkey = self._stop_edit.text().strip()
        cfg.pause_hotkey = self._pause_edit.text().strip()
        cfg.manual_capture_hotkey = self._manual_edit.text().strip()
        cfg.screenshot_mode = self._mode_combo.currentText()
        cfg.output_formats = formats
        cfg.provider = provider
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

        # Re-check on save rather than only at panel build: the token may have
        # been dropped in while this window was open.
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

    # ------------------------------------------------------------------ #
    # Updates — manual only, via GitHub Releases (see updater.py)          #
    # ------------------------------------------------------------------ #

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
        """
        Release-notes dialog with Update now / Not now — this is the whole "let
        people decide if they need the new features" requirement: the release
        body IS the changelog shown here, nothing separate to write.
        """
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
        lay.addWidget(notes, 1)      # the one stretching child

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
        """
        Spawn the detached updater script, then quit AutoDoc immediately — see
        updater.py's module docstring for why the file swap can't happen
        in-process. Nothing after quit_for_update() runs; the relaunch happens
        from the script, not from this process.
        """
        import updater

        self._update_status.setText("Restarting to apply the update…")
        updater.launch_update_and_exit(zip_path)
        if self._quit_for_update is not None:
            self._quit_for_update()
