"""
sessions_window/sessions_window.py — Sessions browser UI.

A simple tkinter window listing all saved sessions. From here the user can:
  • Edit  — opens the built-in session editor (transcript + screenshot review)
  • Generate Documentation — sends the session to Claude and writes a markdown file
  • Delete — removes the session folder from disk

Sessions are displayed by their human-readable name (set during the naming
dialog after stopping). The session ID is shown as a secondary detail.
"""

import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
import tkinter as tk
from tkinter import ttk
from typing import Optional

from config import AutoDocConfig, get_app_dir


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
# Entry point (called from a daemon thread)                           #
# ------------------------------------------------------------------ #

def open_sessions_window(config: AutoDocConfig) -> None:
    """Create a Tk root, build the window, and run the event loop."""
    root = tk.Tk()
    root.title("AutoDoc — Sessions")
    root.geometry("780x420")
    root.minsize(600, 300)
    SessionsWindow(root, config)
    root.mainloop()


# ------------------------------------------------------------------ #
# Window class                                                         #
# ------------------------------------------------------------------ #

class SessionsWindow:
    def __init__(self, root: tk.Tk, config: AutoDocConfig) -> None:
        self.root = root
        self.config = config
        self._sessions: list[dict] = []
        self._generating = False
        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        # ── Toolbar ──────────────────────────────────────────────── #
        toolbar = tk.Frame(self.root, pady=6, padx=8)
        toolbar.pack(fill="x")

        tk.Button(toolbar, text="Refresh", width=10, command=self._refresh).pack(
            side="left", padx=(0, 4)
        )
        tk.Button(
            toolbar, text="Open Output Folder", command=self._open_output_folder
        ).pack(side="left")

        # ── Session list ─────────────────────────────────────────── #
        list_frame = tk.Frame(self.root, padx=8)
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
        self.tree.column("status",       width=90,  anchor="center")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Double-click opens editor
        self.tree.bind("<Double-1>", lambda e: self._edit())

        # ── Action bar ───────────────────────────────────────────── #
        action_bar = tk.Frame(self.root, pady=8, padx=8)
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
            messagebox.showinfo("AutoDoc", "Please select a session first.")
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
            messagebox.showerror("AutoDoc", f"Could not open session:\n{exc}")
            return

        from sessions_window.session_editor import open_editor_window

        threading.Thread(
            target=open_editor_window,
            args=(session_data, session["session_dir"], self.config, self._generate_from_editor),
            daemon=True,
        ).start()

    def _generate_from_editor(self, session_data: dict, session_dir: Path) -> None:
        """Called by the editor when the user clicks Generate inside it."""
        # Reload the session list so counts reflect any deleted screenshots.
        self.root.after(0, self._refresh)
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
            messagebox.showinfo("AutoDoc", "A generation is already in progress.")
            return
        session = self._selected_session()
        if not session:
            return
        self._run_generation(session)

    def _run_generation(self, session: dict) -> None:
        self._generating = True
        self._gen_btn.config(state="disabled", text="Generating…")
        self._status_var.set(f"Generating documentation for '{session['name']}'…")
        self.root.config(cursor="wait")

        threading.Thread(
            target=self._do_generate,
            args=(session,),
            daemon=True,
        ).start()

    def _do_generate(self, session: dict) -> None:
        try:
            from generation.generator import generate_documentation
            from generation.output_writer import write_output

            session_dir: Path = session["session_dir"]
            with open(session_dir / "session.json", encoding="utf-8") as f:
                data = json.load(f)

            markdown = generate_documentation(
                config=self.config,
                session_data=data,
                session_dir=session_dir,
            )
            output_path = write_output(
                markdown=markdown,
                session_id=session["session_id"],
                output_dir=get_app_dir() / self.config.output_dir,
            )
            self.root.after(0, lambda: self._on_generated(output_path))
        except Exception as exc:
            msg = str(exc)
            self.root.after(0, lambda: self._on_generate_error(msg))

    def _on_generated(self, output_path: Path) -> None:
        self._generating = False
        self._gen_btn.config(state="normal", text="Generate Documentation")
        self.root.config(cursor="")
        self._status_var.set(f"Saved: {output_path.name}")
        if messagebox.askyesno(
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}\n\nOpen the file now?",
        ):
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(output_path)])
            else:
                subprocess.Popen(["open", str(output_path)])

    def _on_generate_error(self, error: str) -> None:
        self._generating = False
        self._gen_btn.config(state="normal", text="Generate Documentation")
        self.root.config(cursor="")
        self._status_var.set("Generation failed.")
        messagebox.showerror(
            "AutoDoc — Generation Failed",
            f"Could not generate documentation.\n\nError:\n{error}",
        )

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
            messagebox.showerror("AutoDoc", f"Could not read session:\n{exc}")
            return

        from generation.exporter import export_session, zip_export_folder

        exports_root = get_app_dir() / "exports"
        try:
            export_dir = export_session(session_data, session["session_dir"], exports_root)
            zip_path = zip_export_folder(export_dir)
        except Exception as exc:
            messagebox.showerror("AutoDoc — Export Failed", f"Export error:\n{exc}")
            return

        self._status_var.set(f"Exported: {zip_path.name}")

        if messagebox.askyesno(
            "AutoDoc — Exported",
            f"Session exported to:\n{zip_path}\n\n"
            f"Open the folder containing the zip now?",
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
