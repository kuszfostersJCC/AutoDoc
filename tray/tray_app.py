"""
tray/tray_app.py — System tray icon and application state machine.

States:
    IDLE        → user can Start Recording or open View Sessions
    RECORDING   → user can Stop Recording
    TRANSCRIBING → locked; Whisper is running in a worker thread
    (Sessions window is a separate, non-blocking tkinter window)

pystray runs on the main thread (required by the Win32 backend). The Sessions
window is opened in a separate thread with its own Tk() root — this is not
officially supported by tkinter but works reliably in practice on Windows for
simple windows.

The Whisper model is pre-loaded in a background thread at startup so there is
no delay when the user first stops a recording.
"""

import logging
import sys
import threading
import time
import tkinter as tk
from tkinter import simpledialog
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pystray
from PIL import Image as PILImage

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

_ICON_COLORS = {
    "idle": (70, 130, 180),       # steel blue
    "recording": (220, 50, 47),   # red
    "processing": (255, 165, 0),  # orange
}


def _make_icon(name: str) -> PILImage.Image:
    """Load icon PNG from assets/ or fall back to a colored 64×64 square."""
    path = get_app_dir() / "assets" / f"icon_{name}.png"
    if path.exists():
        return PILImage.open(path).convert("RGBA")
    color = _ICON_COLORS.get(name, (128, 128, 128))
    img = PILImage.new("RGBA", (64, 64), (*color, 255))
    return img


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
        self._icon: Optional[pystray.Icon] = None
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Build the tray icon and block the main thread on pystray's loop."""
        # Kick off Whisper pre-load so it's ready before the first recording ends.
        # Named so it's identifiable in the all-threads stack dump that
        # _transcribe_and_finalize() logs if this preload never finishes.
        threading.Thread(
            target=self._preload_whisper, name="whisper-preload", daemon=True
        ).start()

        self._icon = pystray.Icon(
            "AutoDoc",
            _make_icon("idle"),
            "AutoDoc — Idle",
            menu=self._build_menu(),
        )
        self._icon.run()  # Blocks on main thread (required by Win32 backend).

    # ------------------------------------------------------------------ #
    # Menu construction                                                    #
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> pystray.Menu:
        state = self._state
        if state == "IDLE":
            return pystray.Menu(
                pystray.MenuItem("Start Recording", self._start_recording),
                pystray.MenuItem("Start Transcription Only", self._start_transcript_only),
                pystray.MenuItem("View Sessions", self._open_sessions_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
        if state == "RECORDING":
            mode_label = (
                "Stop Transcription" if self._recording_mode == "transcript_only"
                else "Stop Recording"
            )
            return pystray.Menu(
                pystray.MenuItem(mode_label, self._stop_recording),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
        # TRANSCRIBING
        return pystray.Menu(
            pystray.MenuItem("Processing… please wait", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _apply_state(self, state: str) -> None:
        """Update internal state, tray icon image, tooltip, and menu."""
        self._state = state
        if self._icon is None:
            return
        icon_name = {"IDLE": "idle", "RECORDING": "recording", "TRANSCRIBING": "processing"}.get(
            state, "idle"
        )
        if state == "RECORDING" and self._recording_mode == "transcript_only":
            tooltip = "AutoDoc — Transcription Only…"
        else:
            tooltip = {
                "IDLE": "AutoDoc — Idle",
                "RECORDING": "AutoDoc — Recording…",
                "TRANSCRIBING": "AutoDoc — Transcribing…",
            }.get(state, "AutoDoc")
        self._icon.icon = _make_icon(icon_name)
        self._icon.title = tooltip
        self._icon.menu = self._build_menu()

    # ------------------------------------------------------------------ #
    # Menu actions                                                         #
    # ------------------------------------------------------------------ #

    def _start_recording(self, icon=None, item=None) -> None:
        self._begin_session(mode="full")

    def _start_transcript_only(self, icon=None, item=None) -> None:
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

    def _stop_recording(self, icon=None, item=None) -> None:
        with self._state_lock:
            if self._state != "RECORDING":
                return
            self._apply_state("TRANSCRIBING")

        session = self._session

        # Transcription runs in the background.
        threading.Thread(
            target=self._transcribe_and_finalize,
            args=(session,),
            daemon=True,
        ).start()

        # Naming dialog runs in parallel — user can name while Whisper works.
        threading.Thread(
            target=self._prompt_session_name,
            args=(session,),
            daemon=True,
        ).start()

    def _open_sessions_window(self, icon=None, item=None) -> None:
        from sessions_window.sessions_window import open_sessions_window

        threading.Thread(
            target=open_sessions_window,
            args=(self.config,),
            daemon=True,
        ).start()

    def quit(self) -> None:
        """Clean shutdown — callable from signal handlers or the tray menu."""
        if self._state == "RECORDING" and self._session:
            self._session.stop()
        if self._icon:
            self._icon.stop()

    def _quit(self, icon=None, item=None) -> None:
        self.quit()

    # ------------------------------------------------------------------ #
    # Transcription pipeline                                               #
    # ------------------------------------------------------------------ #

    def _prompt_session_name(self, session: Session) -> None:
        """
        Show a simple naming dialog while transcription runs in the background.
        The name is saved to session.json as soon as the user confirms.
        Runs in its own thread with a short-lived Tk root.
        """
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)   # Bring dialog above other windows.
        name = simpledialog.askstring(
            "Name Your Recording",
            "What workflow did you just record?\n\n"
            "Give it a short, descriptive name\n"
            "(e.g. 'How to submit a grade change'):",
            parent=root,
        )
        root.destroy()
        if name and name.strip():
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
            self._notify("Speech model is still loading — please try again shortly.")
            self._apply_state("IDLE")
            return
        if time.monotonic() - t0 > 1:
            log.info("_transcribe_and_finalize: waited %.1fs for model to finish preloading", time.monotonic() - t0)

        wav_path = session.session_dir / "audio_full.wav"
        try:
            segments = transcribe_audio(
                self._whisper_model,
                wav_path,
                session.started_at_iso,
            )
            session.add_transcript_segments(segments)
        except Exception as exc:
            log.exception("_transcribe_and_finalize: transcription failed")
            self._notify(f"Transcription error — session saved without transcript.\nDetail: {exc}")

        session.finalize(stopped_at)
        self._apply_state("IDLE")
        self._notify("Session saved! Open 'View Sessions' to generate documentation.")

    # ------------------------------------------------------------------ #
    # Notifications                                                        #
    # ------------------------------------------------------------------ #

    def _notify(self, message: str) -> None:
        log.info("_notify: %s", message.replace("\n", " | "))
        if self._icon:
            try:
                self._icon.notify(message, "AutoDoc")
            except Exception:
                # Windows toast notifications are notoriously unreliable for
                # unsigned/portable exes and can silently no-op. The log line
                # above is the one guaranteed place this message actually
                # shows up.
                log.exception("_notify: icon.notify() raised (message above may not have been seen)")
