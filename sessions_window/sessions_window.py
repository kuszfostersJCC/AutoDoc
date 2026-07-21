"""
sessions_window/sessions_window.py — Sessions browser UI.

A tkinter window listing all saved sessions. From here the user can:
  • Edit  — opens the built-in session editor (transcript + screenshot review)
  • Generate Documentation — sends the session to the configured backend
  • Retranscribe — re-runs Whisper on the saved audio (recovery path for
    sessions whose transcription failed or timed out)
  • Export / Delete

THREADING MODEL (do not regress — see ui_service.py for the full story):
open_sessions_window() must be called ON the ui_service UI thread (the tray
schedules it via ui_service.call_soon). The window is a Toplevel of the shared
hidden root — NOT its own Tk() root in its own thread; that pattern caused
hard Tcl-level process crashes. Worker threads (generation, retranscribe)
never touch Tk directly: they report back via ui_service.call_soon. Every
messagebox passes an explicit parent for the same reason.
"""

import json
import logging
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

import ui_service
from config import AutoDocConfig, get_app_dir

log = logging.getLogger(__name__)

# Singleton: re-focus the existing window instead of stacking duplicates.
_open_window: Optional["SessionsWindow"] = None


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
# Entry point (must be called on the ui_service UI thread)             #
# ------------------------------------------------------------------ #

def open_sessions_window(
    config: AutoDocConfig,
    get_model: Optional[Callable[[], object]] = None,
    start_resume: Optional[Callable[[Path], None]] = None,
    is_idle: Optional[Callable[[], bool]] = None,
    quit_for_update: Optional[Callable[[], None]] = None,
) -> None:
    """
    Open (or re-focus) the Sessions window as a Toplevel of the shared root.

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
    if _open_window is not None and _open_window.is_alive():
        _open_window.lift()
        return

    root = ui_service.get_root()
    win = tk.Toplevel(root)
    win.title("AutoDoc — Sessions")
    # Designed at 96 DPI; scaled to the real display (tiny-on-4K fix).
    win.geometry(ui_service.scaled_geometry(980, 560))
    win.minsize(ui_service.scale(760), ui_service.scale(420))
    _open_window = SessionsWindow(win, config, get_model, start_resume, is_idle, quit_for_update)


# ------------------------------------------------------------------ #
# Window class                                                         #
# ------------------------------------------------------------------ #

class SessionsWindow:
    def __init__(
        self,
        root: tk.Toplevel,
        config: AutoDocConfig,
        get_model: Optional[Callable[[], object]] = None,
        start_resume: Optional[Callable[[Path], None]] = None,
        is_idle: Optional[Callable[[], bool]] = None,
        quit_for_update: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.config = config
        self._get_model = get_model
        self._start_resume = start_resume
        self._is_idle = is_idle
        self._quit_for_update = quit_for_update
        self._sessions: list[dict] = []
        self._generating = False
        self._retranscribing = False
        self._build_ui()
        self._refresh()

    def is_alive(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def lift(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        # ── Tabs: Sessions | Settings ────────────────────────────── #
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        sessions_tab = tk.Frame(notebook)
        notebook.add(sessions_tab, text="  Sessions  ")

        from sessions_window.settings_panel import SettingsPanel

        settings_tab = SettingsPanel(
            notebook, self.config, is_idle=self._is_idle, quit_for_update=self._quit_for_update
        )
        notebook.add(settings_tab, text="  Settings  ")

        # ── Toolbar ──────────────────────────────────────────────── #
        toolbar = tk.Frame(sessions_tab, pady=6, padx=8)
        toolbar.pack(fill="x")

        tk.Button(toolbar, text="Refresh", width=10, command=self._refresh).pack(
            side="left", padx=(0, 4)
        )
        tk.Button(
            toolbar, text="Open Output Folder", command=self._open_output_folder
        ).pack(side="left")

        # ── Session list ─────────────────────────────────────────── #
        list_frame = tk.Frame(sessions_tab, padx=8)
        list_frame.pack(fill="both", expand=True)

        cols = ("name", "duration", "screenshots", "segments", "status")
        self.tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse"
        )
        self.tree.heading("name",        text="Session Name")
        self.tree.heading("duration",    text="Duration")
        self.tree.heading("screenshots", text="Screenshots")
        self.tree.heading("segments",    text="Transcript Segments")
        self.tree.heading("status",      text="Status")

        self.tree.column("name",         width=240, anchor="w")
        self.tree.column("duration",     width=80,  anchor="center")
        self.tree.column("screenshots",  width=100, anchor="center")
        self.tree.column("segments",     width=150, anchor="center")
        self.tree.column("status",       width=110, anchor="center")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Double-click opens editor
        self.tree.bind("<Double-1>", lambda e: self._edit())

        # ── Action bar ───────────────────────────────────────────── #
        action_bar = tk.Frame(sessions_tab, pady=8, padx=8)
        action_bar.pack(fill="x")

        self._gen_btn = tk.Button(
            action_bar,
            text="Generate Documentation",
            bg="#2563EB",
            fg="white",
            activebackground="#1D4ED8",
            activeforeground="white",
            width=24,
            command=self._generate,
        )
        self._gen_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            action_bar, text="Edit Session", width=14, command=self._edit
        ).pack(side="left", padx=(0, 6))

        self._retrans_btn = tk.Button(
            action_bar, text="Retranscribe", width=14, command=self._retranscribe
        )
        self._retrans_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            action_bar, text="Record More", width=13, command=self._record_more
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            action_bar, text="Export", width=16, command=self._export
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            action_bar, text="Delete", fg="#DC2626", width=10, command=self._delete
        ).pack(side="right")

        # Status bar
        self._status_var = tk.StringVar(value="")
        tk.Label(
            self.root,
            textvariable=self._status_var,
            anchor="w",
            fg="#6B7280",
            font=("TkDefaultFont", 8),
        ).pack(fill="x", padx=8, pady=(0, 4))

    # ------------------------------------------------------------------ #
    # Data helpers                                                         #
    # ------------------------------------------------------------------ #

    def _refresh(self) -> None:
        self._sessions = _load_sessions(self.config)
        self.tree.delete(*self.tree.get_children())
        for s in self._sessions:
            self.tree.insert(
                "",
                "end",
                values=(
                    s["name"],
                    s["duration"] or "—",
                    s["screenshot_count"],
                    s["segment_count"],
                    s["status"],
                ),
            )
        count = len(self._sessions)
        self._status_var.set(f"{count} session{'s' if count != 1 else ''} found.")

    def _selected_session(self) -> Optional[dict]:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("AutoDoc", "Please select a session first.", parent=self.root)
            return None
        idx = self.tree.index(selection[0])
        return self._sessions[idx]

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
            messagebox.showerror("AutoDoc", f"Could not open session:\n{exc}", parent=self.root)
            return

        from sessions_window.session_editor import open_editor_window

        # Already on the UI thread — open the editor Toplevel directly.
        open_editor_window(
            session_data, session["session_dir"], self.config, self._generate_from_editor
        )

    def _generate_from_editor(self, session_data: dict, session_dir: Path) -> None:
        """Called by the editor when the user clicks Generate inside it."""
        # Reload the session list so counts reflect any deleted screenshots.
        self._refresh()
        # Run generation on the data as saved by the editor.
        self._run_generation(
            {
                "session_id": session_data.get("session_id", session_dir.name),
                "name": session_data.get("name"),
                "session_dir": session_dir,
            }
        )

    def _generate(self) -> None:
        if self._generating:
            messagebox.showinfo("AutoDoc", "A generation is already in progress.", parent=self.root)
            return
        session = self._selected_session()
        if not session:
            return
        self._run_generation(session)

    def _run_generation(self, session: dict) -> None:
        # No wait-cursor: generation runs in a worker thread and the rest of
        # the app stays usable. A "wait" cursor over the whole window told
        # users the opposite. (Note: with a LOCAL model on this same machine,
        # inference can peg every CPU core — the app may still feel sluggish,
        # but that's the model server's load, not blocking in here.)
        self._generating = True
        self._gen_btn.config(state="disabled", text="Generating…")
        self._status_var.set(
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

    def _on_generated(self, output_path: Path) -> None:
        self._generating = False
        self._gen_btn.config(state="normal", text="Generate Documentation")
        self._status_var.set(f"Saved: {output_path.name}")
        if messagebox.askyesno(
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}\n\nOpen the file now?",
            parent=self.root,
        ):
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(output_path)])
            else:
                subprocess.Popen(["open", str(output_path)])

    def _on_generate_error(self, error: str) -> None:
        self._generating = False
        self._gen_btn.config(state="normal", text="Generate Documentation")
        self._status_var.set("Generation failed.")
        messagebox.showerror(
            "AutoDoc — Generation Failed",
            f"Could not generate documentation.\n\nError:\n{error}",
            parent=self.root,
        )

    # ------------------------------------------------------------------ #
    # Retranscribe                                                         #
    # ------------------------------------------------------------------ #

    def _retranscribe(self) -> None:
        """Re-run Whisper on a session's saved audio. Recovery path for
        sessions stuck without a transcript (status 'transcript_failed' or a
        pre-fix 'recording'), but allowed on any session with audio."""
        if self._retranscribing:
            messagebox.showinfo("AutoDoc", "A retranscription is already in progress.", parent=self.root)
            return
        session = self._selected_session()
        if not session:
            return

        wav_path = session["session_dir"] / "audio_full.wav"
        if not wav_path.exists():
            messagebox.showerror(
                "AutoDoc",
                "This session has no saved audio file (audio_full.wav), so it "
                "cannot be retranscribed.",
                parent=self.root,
            )
            return

        model = self._get_model() if self._get_model else None
        if model is None:
            messagebox.showinfo(
                "AutoDoc",
                "The speech model is still loading (or failed to load).\n"
                "Check autodoc.log if this persists, then try again.",
                parent=self.root,
            )
            return

        warning = "This will REPLACE the session's transcript segments."
        if session["segment_count"] == 0:
            warning = "This session currently has no transcript."
        if not messagebox.askyesno(
            "AutoDoc — Retranscribe",
            f"{warning}\n\nAny manual transcript edits will also be cleared.\n"
            f"Retranscribe '{session['name']}' now?",
            parent=self.root,
        ):
            return

        self._retranscribing = True
        self._retrans_btn.config(state="disabled", text="Transcribing…")
        self._status_var.set(f"Retranscribing '{session['name']}'…")

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

    def _on_retranscribed(self, name: str, segment_count: int) -> None:
        self._retranscribing = False
        self._retrans_btn.config(state="normal", text="Retranscribe")
        self._refresh()
        self._status_var.set(f"Retranscribed '{name}': {segment_count} segments.")

    def _on_retranscribe_error(self, error: str) -> None:
        self._retranscribing = False
        self._retrans_btn.config(state="normal", text="Retranscribe")
        self._status_var.set("Retranscription failed.")
        messagebox.showerror(
            "AutoDoc — Retranscribe Failed",
            f"Could not retranscribe this session.\n\nError:\n{error}",
            parent=self.root,
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
            messagebox.showinfo(
                "AutoDoc",
                "Recording can only be started from the main AutoDoc app.",
                parent=self.root,
            )
            return
        if session["status"] == "recording":
            messagebox.showerror(
                "AutoDoc",
                "This session appears to be recording already.",
                parent=self.root,
            )
            return
        if not (session["session_dir"] / "audio_full.wav").exists():
            messagebox.showerror(
                "AutoDoc",
                "This session has no saved audio file, so it cannot be resumed.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "AutoDoc — Record More",
            f"Continue recording into '{session['name']}'?\n\n"
            "New clicks and narration will be added to this session. When you "
            "stop, the transcript is REBUILT from the full audio — any manual "
            "transcript edits will be cleared. Screenshots and their redaction "
            "settings are kept.",
            parent=self.root,
        ):
            return

        self._start_resume(session["session_dir"])
        self._status_var.set(
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
            messagebox.showerror("AutoDoc", f"Could not read session:\n{exc}", parent=self.root)
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
            messagebox.showerror("AutoDoc — Export Failed", f"Export error:\n{exc}", parent=self.root)
            return

        self._status_var.set(f"Exported: {zip_path.name}")

        if messagebox.askyesno(
            "AutoDoc — Exported",
            f"Session exported to:\n{zip_path}\n\n"
            f"Open the folder containing the zip now?",
            parent=self.root,
        ):
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(zip_path)])
            else:
                subprocess.Popen(["open", str(zip_path.parent)])

    def _delete(self) -> None:
        session = self._selected_session()
        if not session:
            return
        if not messagebox.askyesno(
            "AutoDoc",
            f"Permanently delete '{session['name']}'?\n\nThis cannot be undone.",
            parent=self.root,
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
