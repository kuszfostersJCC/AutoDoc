"""
sessions_window/session_editor.py — Session review and editing window.

A split-pane tkinter window that lets users:
  - Edit the transcript as plain text (left panel)
  - Preview and selectively delete screenshots (right panel)
  - Save changes back to session.json
  - Kick off documentation generation directly

Transcript storage:
  The edited text is stored as "edited_full_transcript" in session.json.
  When this key is present, the generation backend uses it in place of the
  individual Whisper segments.

Screenshot deletion:
  Clicking the Delete button on a thumbnail marks that event_id for removal.
  On Save, those events are stripped from session.json. The PNG files are also
  deleted from disk so they don't consume space or tokens.
"""

import json
import threading
from pathlib import Path
from tkinter import messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from PIL import Image, ImageTk


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def open_editor_window(
    session_data: dict,
    session_dir: Path,
    config,
    on_generate: Optional[Callable[[dict, Path], None]] = None,
) -> None:
    """
    Open the editor in a new Tk root (called from a daemon thread).

    Args:
        session_data:  Parsed session.json dict.
        session_dir:   Path to the session folder.
        config:        AutoDocConfig instance.
        on_generate:   Optional callback(session_data, session_dir) invoked
                       when the user clicks Generate from within the editor.
    """
    root = tk.Tk()
    name = session_data.get("name") or session_data.get("session_id", "Session")
    root.title(f"AutoDoc — Edit: {name}")
    root.geometry("1000x620")
    root.minsize(700, 450)
    SessionEditorWindow(root, session_data, session_dir, config, on_generate)
    root.mainloop()


# ------------------------------------------------------------------ #
# Editor window                                                        #
# ------------------------------------------------------------------ #

class SessionEditorWindow:
    THUMB_W = 160
    THUMB_H = 110
    THUMBS_PER_ROW = 3

    def __init__(
        self,
        root: tk.Tk,
        session_data: dict,
        session_dir: Path,
        config,
        on_generate: Optional[Callable[[dict, Path], None]],
    ) -> None:
        self.root = root
        self.session_data = dict(session_data)   # local mutable copy
        self.session_dir = session_dir
        self.config = config
        self._on_generate_cb = on_generate

        self._deleted_ids: set[str] = set()
        self._thumb_refs: list = []   # keeps PhotoImage objects alive
        self._thumb_frames: dict[str, tk.Frame] = {}   # event_id → frame

        self._build_ui()
        self._populate()

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        # ── Header ────────────────────────────────────────────────── #
        header = tk.Frame(self.root, padx=10, pady=6)
        header.pack(fill="x")

        name = self.session_data.get("name") or self.session_data.get("session_id", "")
        tk.Label(
            header,
            text=name,
            font=("TkDefaultFont", 13, "bold"),
            anchor="w",
        ).pack(side="left")

        sid = self.session_data.get("session_id", "")
        tk.Label(
            header,
            text=sid,
            fg="#9CA3AF",
            font=("TkDefaultFont", 9),
            anchor="e",
        ).pack(side="right")

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        # ── Main paned area ──────────────────────────────────────── #
        pane = tk.PanedWindow(self.root, orient="horizontal", sashwidth=6, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=0, pady=0)

        # Left: transcript editor
        left = tk.Frame(pane, padx=8, pady=6)
        pane.add(left, minsize=300, width=460)

        tk.Label(left, text="Transcript", font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            left,
            text="Edit the narration text below. Whisper errors can be corrected here.",
            fg="#6B7280",
            font=("TkDefaultFont", 8),
            anchor="w",
            wraplength=400,
        ).pack(fill="x", pady=(0, 4))

        text_frame = tk.Frame(left)
        text_frame.pack(fill="both", expand=True)

        self.transcript_box = tk.Text(
            text_frame,
            wrap="word",
            font=("TkDefaultFont", 10),
            undo=True,
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=6,
        )
        t_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.transcript_box.yview)
        self.transcript_box.configure(yscrollcommand=t_scroll.set)
        self.transcript_box.pack(side="left", fill="both", expand=True)
        t_scroll.pack(side="right", fill="y")

        # Right: screenshot thumbnails — or a plain notice for transcript-only sessions
        right = tk.Frame(pane, padx=8, pady=6)
        is_transcript_only = self.session_data.get("session_type") == "transcript_only"

        if is_transcript_only:
            pane.add(right, minsize=200, width=300)
            tk.Label(right, text="Screenshots", font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(
                right,
                text=(
                    "This is a transcript-only session.\n\n"
                    "No screenshots were captured — narration alone will be sent for Documentation."
                ),
                fg="#6B7280",
                font=("TkDefaultFont", 9),
                anchor="nw",
                justify="left",
                wraplength=260,
            ).pack(fill="x", pady=(8, 0))
            # Stubs so the rest of the class doesn't need special-casing
            self._canvas = tk.Canvas(right)
            self._thumb_container = tk.Frame(self._canvas)
            self._canvas_window = None
        else:
            pane.add(right, minsize=260, width=500)
            tk.Label(right, text="Screenshots", font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(
                right,
                text="Click Delete to remove a screenshot. Tip: remove the last few stop-click frames.",
                fg="#6B7280",
                font=("TkDefaultFont", 8),
                anchor="w",
                wraplength=360,
            ).pack(fill="x", pady=(0, 4))

            canvas_frame = tk.Frame(right)
            canvas_frame.pack(fill="both", expand=True)

            self._canvas = tk.Canvas(canvas_frame, highlightthickness=0)
            v_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self._canvas.yview)
            self._canvas.configure(yscrollcommand=v_scroll.set)

            self._canvas.pack(side="left", fill="both", expand=True)
            v_scroll.pack(side="right", fill="y")

            self._thumb_container = tk.Frame(self._canvas)
            self._canvas_window = self._canvas.create_window(
                (0, 0), window=self._thumb_container, anchor="nw"
            )
            self._thumb_container.bind(
                "<Configure>",
                lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
            )
            self._canvas.bind("<Configure>", self._on_canvas_resize)
            self._canvas.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        # ── Footer / action bar ──────────────────────────────────── #
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")
        footer = tk.Frame(self.root, padx=10, pady=8)
        footer.pack(fill="x")

        self._status_var = tk.StringVar(value="")
        tk.Label(footer, textvariable=self._status_var, fg="#6B7280", font=("TkDefaultFont", 8)).pack(side="left")

        tk.Button(
            footer,
            text="Generate Documentation",
            bg="#2563EB",
            fg="white",
            activebackground="#1D4ED8",
            activeforeground="white",
            padx=10,
            command=self._save_and_generate,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            footer,
            text="Save Changes",
            padx=10,
            command=self._save,
        ).pack(side="right")

    def _on_canvas_resize(self, event: tk.Event) -> None:
        """Keep the inner frame width locked to the canvas width."""
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    # ------------------------------------------------------------------ #
    # Data population                                                      #
    # ------------------------------------------------------------------ #

    def _populate(self) -> None:
        events = self.session_data.get("events", [])

        # ── Transcript ─────────────────────────────────────────── #
        if "edited_full_transcript" in self.session_data:
            transcript_text = self.session_data["edited_full_transcript"]
        else:
            segments = sorted(
                [e for e in events if e.get("type") == "transcript_segment"],
                key=lambda e: e["timestamp_offset_ms"],
            )
            transcript_text = "\n\n".join(
                (e.get("edited_text") or e["data"]["text"]).strip()
                for e in segments
            )

        self.transcript_box.insert("1.0", transcript_text)

        # ── Screenshots ────────────────────────────────────────── #
        screenshots = sorted(
            [e for e in events if e.get("type") == "screenshot"],
            key=lambda e: e["timestamp_offset_ms"],
        )
        for evt in screenshots:
            self._add_thumbnail(evt)

        count = len(screenshots)
        self._status_var.set(f"{count} screenshot{'s' if count != 1 else ''} • Edit transcript then save or generate.")

    def _add_thumbnail(self, event: dict) -> None:
        img_path = self.session_dir / event["data"]["filename"]
        if not img_path.exists():
            return

        # Load + resize image for display.
        # master=self.root is critical when multiple Tk() instances exist —
        # without it, PIL binds the PhotoImage to the first Tk interpreter
        # (the Sessions window), and the editor's Tcl interpreter rejects it.
        try:
            img = Image.open(img_path)
            img.thumbnail((self.THUMB_W, self.THUMB_H), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img, master=self.root)
            self._thumb_refs.append(photo)
        except Exception:
            return

        # Place in a grid: THUMBS_PER_ROW columns
        idx = len(self._thumb_frames)
        row, col = divmod(idx, self.THUMBS_PER_ROW)

        cell = tk.Frame(
            self._thumb_container,
            bd=1,
            relief="solid",
            padx=3,
            pady=3,
            bg="white",
        )
        cell.grid(row=row, column=col, padx=5, pady=5, sticky="n")

        # cursor="hand2" signals the image is clickable (expand on click).
        img_label = tk.Label(cell, image=photo, bg="white", cursor="hand2")
        img_label.pack()
        img_label.bind("<Button-1>", lambda e, p=img_path: self._show_full_image(p))

        # Timestamp + click type label
        offset_s = event["timestamp_offset_ms"] / 1000
        click = event["data"].get("click_type", "left")
        tk.Label(
            cell,
            text=f"{offset_s:.1f}s  •  {click}-click  •  click to expand",
            font=("TkDefaultFont", 7),
            fg="#6B7280",
            bg="white",
        ).pack()

        eid = event["event_id"]
        delete_btn = tk.Button(
            cell,
            text="Delete",
            fg="#DC2626",
            font=("TkDefaultFont", 8),
            relief="flat",
            command=lambda e=eid, f=cell: self._toggle_delete(e, f),
        )
        delete_btn.pack(pady=(2, 0))
        # Store refs so toggle_delete and save can update them.
        cell._delete_btn = delete_btn
        cell._img_label = img_label
        cell._img_path = img_path

        self._thumb_frames[eid] = cell

    def _toggle_delete(self, event_id: str, frame: tk.Frame) -> None:
        if event_id in self._deleted_ids:
            # Restore
            self._deleted_ids.discard(event_id)
            frame.configure(bg="white")
            frame._img_label.configure(bg="white")
            frame._delete_btn.configure(text="Delete", fg="#DC2626")
        else:
            # Mark for deletion
            self._deleted_ids.add(event_id)
            frame.configure(bg="#FEE2E2")
            frame._img_label.configure(bg="#FEE2E2")
            frame._delete_btn.configure(text="Restore", fg="#059669")

        remaining = len(self._thumb_frames) - len(self._deleted_ids)
        self._status_var.set(f"{remaining} screenshot(s) will be sent for Documentation.")

    # ------------------------------------------------------------------ #
    # Save                                                                 #
    # ------------------------------------------------------------------ #

    def _save(self) -> bool:
        """
        Persist edits to session.json. Returns True on success.
        - Removes deleted screenshot events (and their PNG files).
        - Stores the edited transcript as edited_full_transcript.
        """
        edited_text = self.transcript_box.get("1.0", "end").strip()
        events = self.session_data.get("events", [])

        # Remove deleted screenshot events and their files
        kept_events = []
        for evt in events:
            if evt.get("type") == "screenshot" and evt["event_id"] in self._deleted_ids:
                # Delete the PNG from disk
                png = self.session_dir / evt["data"]["filename"]
                try:
                    png.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                kept_events.append(evt)

        self.session_data["events"] = kept_events
        self.session_data["edited_full_transcript"] = edited_text

        # Remove deleted thumbnail frames from the UI immediately.
        for eid in list(self._deleted_ids):
            frame = self._thumb_frames.pop(eid, None)
            if frame:
                frame.destroy()
        self._deleted_ids.clear()

        # Re-grid the surviving thumbnails to fill gaps left by deletions.
        self._regrid_thumbnails()

        json_path = self.session_dir / "session.json"
        tmp = json_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.session_data, f, indent=2, ensure_ascii=False)
            tmp.replace(json_path)
        except OSError as exc:
            messagebox.showerror("AutoDoc", f"Could not save session:\n{exc}")
            return False

        remaining = len(self._thumb_frames)
        self._status_var.set(
            f"Changes saved. {remaining} screenshot{'s' if remaining != 1 else ''} remaining."
        )
        return True

    def _regrid_thumbnails(self) -> None:
        """Re-assign grid positions after deletions to eliminate visual gaps."""
        for idx, frame in enumerate(self._thumb_frames.values()):
            row, col = divmod(idx, self.THUMBS_PER_ROW)
            frame.grid(row=row, column=col, padx=5, pady=5, sticky="n")
        # Shrink the canvas scroll region to match the new content size.
        self._thumb_container.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    # ------------------------------------------------------------------ #
    # Full-size image preview                                              #
    # ------------------------------------------------------------------ #

    def _show_full_image(self, img_path: Path) -> None:
        """Open a Toplevel popup showing the screenshot at a readable size."""
        try:
            img = Image.open(img_path)
        except Exception as exc:
            messagebox.showerror("AutoDoc", f"Could not open image:\n{exc}", parent=self.root)
            return

        # Scale to fit within 1200×800 without upscaling.
        max_w, max_h = 1200, 800
        ratio = min(max_w / img.width, max_h / img.height, 1.0)
        if ratio < 1.0:
            img = img.resize(
                (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS
            )

        popup = tk.Toplevel(self.root)
        popup.title(f"Screenshot — {img_path.name}")
        popup.resizable(True, True)

        # PhotoImage must be bound to the popup's interpreter context.
        photo = ImageTk.PhotoImage(img, master=popup)
        popup._photo = photo   # Prevent garbage collection.

        tk.Label(popup, image=photo).pack()
        tk.Button(popup, text="Close", command=popup.destroy, padx=12, pady=4).pack(pady=6)
        popup.focus_set()

    # ------------------------------------------------------------------ #
    # Generate                                                             #
    # ------------------------------------------------------------------ #

    def _save_and_generate(self) -> None:
        if not self._save():
            return

        if self._on_generate_cb:
            # Delegate to the sessions window's generation pipeline
            self._on_generate_cb(self.session_data, self.session_dir)
            self.root.destroy()
        else:
            # Standalone: run generation here
            self._run_generation_inline()

    def _run_generation_inline(self) -> None:
        self.root.config(cursor="wait")
        self._status_var.set("Generating documentation…")

        def _work():
            try:
                from generation.generator import generate_documentation
                from generation.output_writer import write_output
                from config import get_app_dir

                markdown = generate_documentation(
                    config=self.config,
                    session_data=self.session_data,
                    session_dir=self.session_dir,
                )
                output_path = write_output(
                    markdown=markdown,
                    session_id=self.session_data.get("session_id", "session"),
                    output_dir=get_app_dir() / self.config.output_dir,
                )
                self.root.after(0, lambda: self._on_done(output_path))
            except Exception as exc:
                msg = str(exc)
                self.root.after(0, lambda: self._on_error(msg))

        threading.Thread(target=_work, daemon=True).start()

    def _on_done(self, output_path: Path) -> None:
        self.root.config(cursor="")
        self._status_var.set(f"Saved: {output_path.name}")
        if messagebox.askyesno("AutoDoc — Done", f"Documentation saved to:\n{output_path}\n\nOpen now?"):
            import subprocess, sys
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(output_path)])
            else:
                subprocess.Popen(["open", str(output_path)])

    def _on_error(self, error: str) -> None:
        self.root.config(cursor="")
        self._status_var.set("Generation failed.")
        messagebox.showerror("AutoDoc — Error", f"Could not generate documentation:\n\n{error}")
