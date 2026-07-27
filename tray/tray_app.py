"""
tray/tray_app.py — System tray icon and application state machine.

States:
    IDLE         → user can Start Recording or open View Sessions
    RECORDING    → user can Stop Recording / Pause
    TRANSCRIBING → locked; Whisper is running in a worker thread

THREADING (changed 2026-07-24, Qt port):
Previously pystray owned the process main thread (its Win32 backend requires
that) and tkinter ran a second event loop on its own thread — two message
pumps, and the reason ui_service.py existed in its original form. Now
QSystemTrayIcon is just another Qt widget: the tray, every window, and every
dialog all live on the single Qt main thread, and there is exactly one event
loop. pystray is gone as a dependency.

Worker threads (recording, transcription, generation, the pynput hotkey
listener) still never touch UI objects directly — they hand callables to
ui_service.call_soon(). That constraint did not relax; only its enforcement
mechanism changed. See ui_service.py.

The Whisper model is pre-loaded in a background thread at startup so there is
no delay when the user first stops a recording.
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QInputDialog, QMenu, QSystemTrayIcon

import ui_service
from config import AutoDocConfig, get_app_dir
from recorder.session import Session
from transcription.whisper_runner import (
    is_model_cached,
    load_whisper_model,
    transcribe_audio,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Icon helpers                                                         #
# ------------------------------------------------------------------ #

def _make_icon(name: str) -> QIcon:
    """
    Tray icon for a state, from assets/icon_<name>.(svg|png).

    SVG is preferred (crisp at every tray DPI — the style guide's §7 rule
    against runtime-scaled rasters applies here too); .png is accepted as a
    fallback so the existing assets keep working. If neither exists Qt gets a
    null QIcon, which shows nothing in the tray — hence the warning, because
    "my tray icon is invisible" is otherwise a mystery.
    """
    assets = get_app_dir() / "assets"
    for ext in ("svg", "png"):
        path = assets / f"icon_{name}.{ext}"
        if path.exists():
            return QIcon(str(path))
    log.warning("_make_icon: no assets/icon_%s.{svg,png} found — tray icon will be blank", name)
    return QIcon()


# ------------------------------------------------------------------ #
# TrayApp                                                              #
# ------------------------------------------------------------------ #

class TrayApp:
    def __init__(self, config: AutoDocConfig) -> None:
        self.config = config
        self._session: Optional[Session] = None
        self._whisper_model = None
        self._whisper_ready = threading.Event()
        self._state = "IDLE"
        self._recording_mode: str = "full"   # "full" | "transcript_only"
        self._tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._state_lock = threading.Lock()
        self._hotkeys = None  # pynput GlobalHotKeys, active only while recording
        self._sessions_window = None

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """
        Create the tray icon and menu. Returns immediately — unlike pystray's
        blocking icon.run(), the event loop is QApplication.exec() in main().
        """
        self._tray = QSystemTrayIcon(_make_icon("idle"))
        self._menu = QMenu()
        self._tray.setContextMenu(self._menu)
        self._apply_state("IDLE")
        self._tray.show()

        # Kick off Whisper pre-load so it's ready before the first recording
        # ends. Named so it's identifiable in the all-threads stack dump that
        # _transcribe_and_finalize() logs if this preload never finishes.
        threading.Thread(
            target=self._preload_whisper, name="whisper-preload", daemon=True
        ).start()

    # ------------------------------------------------------------------ #
    # Menu construction                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pretty_hotkey(combo: str) -> str:
        """'<ctrl>+<shift>+p' → ' (Ctrl+Shift+P)' suffix for menu labels."""
        combo = (combo or "").strip()
        if not combo:
            return ""
        pretty = "+".join(p.strip("<>").capitalize() for p in combo.split("+"))
        return f" ({pretty})"

    def _rebuild_menu(self) -> None:
        """
        Repopulate the tray menu for the current state.

        QMenu is rebuilt in place (clear() + re-add) rather than swapped for a
        new object: QSystemTrayIcon.setContextMenu does not take ownership, so
        replacing the menu each time would leave the old QMenu to be garbage
        collected while Windows may still reference it. Keeping one QMenu for
        the tray's lifetime avoids that class of crash entirely.
        """
        menu = self._menu
        if menu is None:
            return
        menu.clear()

        state = self._state
        if state == "IDLE":
            self._add_action(menu, "Start recording", self._start_recording)
            self._add_action(menu, "Start transcription only", self._start_transcript_only)
            self._add_action(menu, "View sessions", self._open_sessions_window)
            menu.addSeparator()
            self._add_action(menu, "Quit", self.quit)
            return

        if state == "RECORDING":
            mode_label = (
                "Stop transcription" if self._recording_mode == "transcript_only"
                else "Stop recording"
            ) + self._pretty_hotkey(self.config.stop_hotkey)
            paused = bool(self._session and self._session.is_paused)
            pause_label = (
                "Resume capture" if paused else "Pause capture"
            ) + self._pretty_hotkey(self.config.pause_hotkey)
            self._add_action(menu, mode_label, self._stop_recording)
            self._add_action(menu, pause_label, self._toggle_pause)
            menu.addSeparator()
            self._add_action(menu, "Quit", self.quit)
            return

        # TRANSCRIBING
        busy = self._add_action(menu, "Processing… please wait", None)
        busy.setEnabled(False)
        menu.addSeparator()
        self._add_action(menu, "Quit", self.quit)

    def _add_action(self, menu: QMenu, label: str, slot) -> QAction:
        action = QAction(label, menu)
        if slot is not None:
            action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _apply_state(self, state: str) -> None:
        """
        Update internal state, tray icon, tooltip, and menu.

        Callable from worker threads (the transcription thread and the pynput
        hotkey thread both do), so all Qt work is marshalled to the main
        thread via ui_service.call_soon. The plain attribute assignment stays
        here so is_idle()/_state checks see the new value immediately rather
        than after a trip through the event loop.
        """
        self._state = state

        def _apply() -> None:
            if self._tray is None:
                return
            icon_name = {
                "IDLE": "idle", "RECORDING": "recording", "TRANSCRIBING": "processing",
            }.get(state, "idle")
            if state == "RECORDING" and self._session and self._session.is_paused:
                tooltip = "AutoDoc — Paused"
            elif state == "RECORDING" and self._recording_mode == "transcript_only":
                tooltip = "AutoDoc — Transcription only…"
            else:
                tooltip = {
                    "IDLE": "AutoDoc — Idle",
                    "RECORDING": "AutoDoc — Recording…",
                    "TRANSCRIBING": "AutoDoc — Transcribing…",
                }.get(state, "AutoDoc")
            self._tray.setIcon(_make_icon(icon_name))
            self._tray.setToolTip(tooltip)
            self._rebuild_menu()

        ui_service.call_soon(_apply)

    # ------------------------------------------------------------------ #
    # Menu actions                                                         #
    # ------------------------------------------------------------------ #

    def _start_recording(self) -> None:
        self._begin_session(mode="full")

    def _start_transcript_only(self) -> None:
        self._begin_session(mode="transcript_only")

    def _begin_session(self, mode: str) -> None:
        with self._state_lock:
            if self._state != "IDLE":
                return
            self._recording_mode = mode
            self._apply_state("RECORDING")

        self._session = Session(self.config)
        try:
            self._session.start(mode=mode)
        except RuntimeError as exc:
            self._notify(str(exc))
            self._recording_mode = "full"
            self._apply_state("IDLE")
            self._session = None
            return

        # Pause/stop/manual-capture hotkeys all apply in every mode now —
        # see _start_hotkey_listener()'s docstring.
        self._start_hotkey_listener()

    # ------------------------------------------------------------------ #
    # Pause / manual-capture hotkey (PRD F4)                               #
    # ------------------------------------------------------------------ #

    def _toggle_pause(self) -> None:
        session = self._session
        if not session or self._state != "RECORDING":
            return
        if session.is_paused:
            session.resume()
            log.info("recording resumed")
        else:
            session.pause()
            log.info("recording paused")
        self._apply_state("RECORDING")  # refresh menu label + tooltip

    def _start_hotkey_listener(self) -> None:
        """
        Register recording hotkeys: pause/resume, stop, and manual capture —
        all three apply in every mode. Manual (hotkey-triggered) capture and
        auto-capture-on-click are independent concerns: "Transcription Only"
        only disables the latter (recorder/session.py never starts
        MouseHooks for it), so a manual capture is still possible mid-
        recording even with auto-capture off (e.g. narrating in front of a
        screen while occasionally hotkeying a shot). One listener for all
        three; active only while recording. The stop hotkey exists so ending
        a recording doesn't pollute it with tray-clicking screenshots.
        """
        combos = {}
        pause = (self.config.pause_hotkey or "").strip()
        stop = (self.config.stop_hotkey or "").strip()
        manual = (self.config.manual_capture_hotkey or "").strip()
        if pause:
            combos[pause] = self._on_hotkey_pause
        if stop:
            if stop in combos:
                log.warning("stop_hotkey %r collides with pause_hotkey — stop disabled", stop)
            else:
                combos[stop] = self._on_hotkey_stop
        if manual:
            if manual in combos:
                log.warning("manual_capture_hotkey %r collides with another hotkey — disabled", manual)
            else:
                combos[manual] = self._on_manual_capture
        if not combos:
            return
        try:
            from pynput import keyboard

            self._hotkeys = keyboard.GlobalHotKeys(combos)
            self._hotkeys.start()
            log.info("recording hotkeys active: %s", ", ".join(combos))
        except Exception:
            # Bad combo string or hook failure — recording works without it.
            self._hotkeys = None
            log.exception(
                "recording hotkeys could not be registered (%s) — check the "
                "hotkey settings (pynput syntax, e.g. '<ctrl>+<shift>+s')",
                ", ".join(combos),
            )

    def _stop_hotkey_listener(self) -> None:
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:
                log.exception("hotkey listener stop failed")
            self._hotkeys = None

    def _on_manual_capture(self) -> None:
        session = self._session
        if session and self._state == "RECORDING":
            session.manual_capture()

    def _on_hotkey_pause(self) -> None:
        # Runs on the pynput listener thread. _toggle_pause only touches
        # Session (thread-safe by design) and _apply_state, which marshals its
        # own Qt work — so this is safe without extra wrapping.
        self._toggle_pause()

    def _on_hotkey_stop(self) -> None:
        # No screenshots fire for a keystroke, so the recording ends clean —
        # no trailing "clicks that only stopped the recording" frames.
        log.info("stop hotkey pressed")
        self._stop_recording()

    def _stop_recording(self) -> None:
        with self._state_lock:
            if self._state != "RECORDING":
                return
            self._apply_state("TRANSCRIBING")

        self._stop_hotkey_listener()
        session = self._session

        # Transcription runs in the background.
        threading.Thread(
            target=self._transcribe_and_finalize,
            args=(session,),
            daemon=True,
        ).start()

        # Naming dialog runs in parallel on the main thread — user can name
        # while Whisper works. Resumed sessions already have a name (PRD F5),
        # so don't nag for a new one.
        if session and not session.name:
            ui_service.call_soon(lambda: self._prompt_session_name(session))

    def get_whisper_model(self):
        """Loaded Whisper model, or None if the preload hasn't finished/failed.
        Handed to the Sessions window so Retranscribe reuses the tray's model
        instead of loading a duplicate (~memory of a second full model)."""
        return self._whisper_model if self._whisper_ready.is_set() else None

    def resume_recording(self, session_dir) -> None:
        """
        Record More (PRD F5): append a new recording pass onto an existing
        session. Called from the Sessions window on the main thread; only
        TrayApp may start recordings because it owns the state machine.
        """
        session_dir = Path(session_dir)
        with self._state_lock:
            if self._state != "IDLE":
                self._notify("Finish the current recording before resuming another session.")
                return
            try:
                with open(session_dir / "session.json", encoding="utf-8") as f:
                    self._recording_mode = json.load(f).get("session_type", "full")
            except Exception:
                self._recording_mode = "full"
            self._apply_state("RECORDING")

        self._session = Session(self.config)
        try:
            self._session.start(mode=self._recording_mode, resume_dir=session_dir)
        except Exception as exc:
            log.exception("resume_recording: failed to start")
            self._notify(f"Could not resume this session:\n{exc}")
            self._recording_mode = "full"
            self._apply_state("IDLE")
            self._session = None
            return

        self._start_hotkey_listener()
        self._notify("Recording more into this session — stop from the tray icon when done.")

    def _open_sessions_window(self) -> None:
        from sessions_window.sessions_window import open_sessions_window

        ui_service.call_soon(
            lambda: open_sessions_window(
                self.config,
                get_model=self.get_whisper_model,
                start_resume=self.resume_recording,
                is_idle=self.is_idle,
                quit_for_update=self.quit,
            )
        )

    def is_idle(self) -> bool:
        """Whether it's safe to apply an update — see Settings tab's "Check
        for Updates". Recording/transcribing mid-update risks corrupting the
        in-progress session, so that flow refuses to proceed unless this is
        True."""
        return self._state == "IDLE"

    def quit(self) -> None:
        """Clean shutdown — callable from signal handlers or the tray menu."""
        self._stop_hotkey_listener()
        if self._state == "RECORDING" and self._session:
            self._session.stop()

        def _teardown() -> None:
            from PySide6.QtWidgets import QApplication

            if self._tray is not None:
                # Hide before quitting or Windows can leave a phantom icon in
                # the notification area until the user hovers over it.
                self._tray.hide()
            QApplication.instance().quit()

        ui_service.call_soon(_teardown)

    # ------------------------------------------------------------------ #
    # Transcription pipeline                                               #
    # ------------------------------------------------------------------ #

    def _prompt_session_name(self, session: Session) -> None:
        """
        Show a simple naming dialog while transcription runs in the background.
        The name is saved to session.json as soon as the user confirms.
        MUST run on the Qt main thread (scheduled via call_soon) — under the
        old tkinter stack this used to spin up its own Tk() root on its own
        thread, which hard-crashed the process; the Qt equivalent of that
        mistake corrupts paint state instead of crashing outright, which is
        worse to diagnose.

        Parent is the Sessions window if it happens to be open, else None.
        A modal dialog here is fine: it's brief, and no other window needs
        interaction while it's up.
        """
        name, ok = QInputDialog.getText(
            ui_service.get_main_window(),
            "Name your recording",
            "What workflow did you just record?\n\n"
            "Give it a short, descriptive name\n"
            "(e.g. 'How to submit a grade change'):",
        )
        if ok and name and name.strip():
            session.set_name(name.strip())

    def _preload_whisper(self) -> None:
        """Load the Whisper model in the background at startup."""
        t0 = time.monotonic()
        first_download = not is_model_cached(self.config.whisper_model)
        log.info("_preload_whisper: model=%r first_download=%s", self.config.whisper_model, first_download)
        if first_download:
            self._notify(
                f"Downloading speech model (~150 MB) — first-time setup.\n"
                "AutoDoc will notify you when it's ready."
            )
        try:
            self._whisper_model = load_whisper_model(self.config.whisper_model)
            self._whisper_ready.set()
            log.info("_preload_whisper: ready after %.1fs total", time.monotonic() - t0)
            if first_download:
                self._notify("Speech model downloaded. AutoDoc is ready to use!")
        except Exception as exc:
            # Broad on purpose. load_whisper_model() wraps its known failure
            # modes in RuntimeError, but anything it *didn't* anticipate
            # (OSError from a DLL, MemoryError, ...) used to escape here and
            # kill this daemon thread with no trace in the log — observed
            # 2026-07-08 on a target PC: the log simply stopped after the
            # line above, and the only downstream symptom was the 300s
            # model-wait timeout. Never let that happen again.
            log.exception("_preload_whisper: failed after %.1fs", time.monotonic() - t0)
            self._notify(f"Speech model failed to load:\n{exc}")

    @staticmethod
    def _dump_all_thread_stacks() -> None:
        """
        Log the current Python stack of every live thread.

        Called when the model-preload wait times out, to distinguish the two
        possible causes conclusively from the log alone:
          - HANG: a thread named 'whisper-preload' appears below, and its
            stack shows the exact line it's stuck on (e.g. inside
            `import torch` DLL loading, or whisper's urlopen with no timeout).
          - DEAD THREAD: no 'whisper-preload' thread appears below — it died.
            Its traceback will be in the log earlier (via _preload_whisper's
            except clause or main.py's threading.excepthook).
        """
        import traceback

        frames = sys._current_frames()
        lines = ["_dump_all_thread_stacks: %d live threads" % len(frames)]
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            lines.append(
                "--- thread %r (ident=%s, daemon=%s) ---"
                % (thread.name, thread.ident, thread.daemon)
            )
            if frame is None:
                lines.append("    <no frame — thread finished>")
            else:
                lines.append("".join(traceback.format_stack(frame)).rstrip())
        log.warning("\n".join(lines))

    def _transcribe_and_finalize(self, session: Session) -> None:
        stopped_at = datetime.now(timezone.utc)
        session.stop()

        # Wait up to 5 min for the model; it should already be ready in most cases.
        t0 = time.monotonic()
        if not self._whisper_ready.wait(timeout=300):
            log.warning(
                "_transcribe_and_finalize: gave up waiting for the model after %.1fs "
                "without _preload_whisper ever calling _whisper_ready.set(). Look at the "
                "_preload_whisper log lines above (or their absence) to see whether "
                "load_whisper_model() is still hung inside whisper.load_model()/torch.load(), "
                "or whether it raised and the tray notification just didn't get seen.",
                time.monotonic() - t0,
            )
            self._dump_all_thread_stacks()
            # CRITICAL: finalize anyway. Returning without finalize() left the
            # session stuck at status "recording" forever (observed bug).
            # transcript_ok=False marks it "transcript_failed", which the
            # Sessions window surfaces with a Retranscribe option.
            session.finalize(stopped_at, transcript_ok=False)
            self._notify(
                "Session saved WITHOUT a transcript (speech model wasn't ready).\n"
                "Use 'Retranscribe' in View Sessions to try again."
            )
            self._apply_state("IDLE")
            return
        if time.monotonic() - t0 > 1:
            log.info("_transcribe_and_finalize: waited %.1fs for model to finish preloading", time.monotonic() - t0)

        wav_path = session.session_dir / "audio_full.wav"
        transcript_ok = True
        try:
            segments = transcribe_audio(
                self._whisper_model,
                wav_path,
                session.started_at_iso,
            )
            session.add_transcript_segments(segments)
        except Exception as exc:
            transcript_ok = False
            log.exception("_transcribe_and_finalize: transcription failed")
            self._notify(
                f"Transcription error — session saved without transcript.\n"
                f"Use 'Retranscribe' in View Sessions to try again.\nDetail: {exc}"
            )

        session.finalize(stopped_at, transcript_ok=transcript_ok)
        self._apply_state("IDLE")
        if transcript_ok:
            self._notify("Session saved! Open 'View sessions' to generate documentation.")

    # ------------------------------------------------------------------ #
    # Notifications                                                        #
    # ------------------------------------------------------------------ #

    def _notify(self, message: str) -> None:
        """
        Tray balloon notification. Safe to call from any thread — the Qt call
        is marshalled to the main thread.
        """
        log.info("_notify: %s", message.replace("\n", " | "))

        def _show() -> None:
            if self._tray is None:
                return
            try:
                self._tray.showMessage("AutoDoc", message, _make_icon("idle"))
            except Exception:
                # Windows toast/balloon delivery is unreliable for
                # unsigned/portable exes and can silently no-op. The log line
                # above is the one guaranteed place this message shows up.
                log.exception("_notify: showMessage raised (message above may not have been seen)")

        ui_service.call_soon(_show)
