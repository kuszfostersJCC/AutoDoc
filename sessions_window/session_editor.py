"""
sessions_window/session_editor.py — Session review and editing window.

A split-pane tkinter window that lets users:
  - Edit the transcript as plain text (left panel)
  - Preview and selectively delete screenshots (right panel)
  - Choose how screenshots are rendered — raw / blurred / circled — with a
    live re-render of the thumbnails (right panel header)
  - Add externally-sourced photos to the session (e.g. a phone photo of a
    BIOS screen AutoDoc physically cannot screenshot) via recorder/
    session.py::import_screenshots()
  - Save changes back to session.json
  - Kick off documentation generation directly

Screenshot panel visibility (changed 2026-07-20):
  The panel is now ALWAYS the full thumbnail view, never a static "no
  screenshots" notice keyed off session_type == "transcript_only". A
  session's session_type only ever controlled whether recorder/session.py
  auto-captured clicks — it was never a hard guarantee of zero screenshots,
  and now that photos can be imported after the fact, that guarantee would
  be actively wrong. Any UI/export logic that needs to know "does this
  session have screenshots" checks the actual event list (screenshot_count),
  not the mode label. See HANDOFF.md for the full rationale.

Transcript storage:
  The edited text is stored as "edited_full_transcript" in session.json.
  When this key is present, the generation backend uses it in place of the
  individual Whisper segments.

Screenshot rendering:
  Screenshots are stored RAW on disk (see recorder/screenshot.py). The mode
  chosen here is stored as "screenshot_render_mode" in session.json and is
  applied at preview/export/generation time — switching it is always
  reversible. Legacy sessions (pre-raw-storage) have destructively blurred
  PNGs and no click coordinates, so the selector is disabled for them.

Screenshot deletion:
  Clicking the Delete button on a thumbnail marks that event_id for removal.
  On Save, those events are stripped from session.json. The PNG files are also
  deleted from disk so they don't consume space or tokens.

THREADING MODEL (do not regress — see ui_service.py):
open_editor_window() must be called ON the ui_service UI thread. The window
is a Toplevel of the shared hidden root — never its own Tk() root in its own
thread. Worker threads report back via ui_service.call_soon only.
"""

import json
import logging
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

import ui_service
from recorder.screenshot import (
    RENDER_MODES,
    event_is_legacy,
    render_event_image,
    resolve_render_mode,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Entry point (must be called on the ui_service UI thread)             #
# ------------------------------------------------------------------ #

def open_editor_window(
    session_data: dict,
    session_dir: Path,
    config,
    on_generate: Optional[Callable[[dict, Path], None]] = None,
) -> None:
    """
    Open the editor as a Toplevel of the shared UI root.

    Args:
        session_data:  Parsed session.json dict.
        session_dir:   Path to the session folder.
        config:        AutoDocConfig instance.
        on_generate:   Optional callback(session_data, session_dir) invoked
                       when the user clicks Generate from within the editor.
    """
    root = tk.Toplevel(ui_service.get_root())
    name = session_data.get("name") or session_data.get("session_id", "Session")
    root.title(f"AutoDoc — Edit: {name}")
    root.geometry(ui_service.scaled_geometry(1000, 620))
    root.minsize(ui_service.scale(700), ui_service.scale(450))
    SessionEditorWindow(root, session_data, session_dir, config, on_generate)


# ------------------------------------------------------------------ #
# Editor window                                                        #
# ------------------------------------------------------------------ #

class SessionEditorWindow:
    THUMB_W = 160
    THUMB_H = 110
    THUMBS_PER_ROW = 3

    def __init__(
        self,
        root: tk.Toplevel,
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

        self._screenshot_events = sorted(
            [e for e in self.session_data.get("events", []) if e.get("type") == "screenshot"],
            key=lambda e: e["timestamp_offset_ms"],
        )
        self._all_legacy = bool(self._screenshot_events) and all(
            event_is_legacy(e) for e in self._screenshot_events
        )
        self._render_mode = resolve_render_mode(self.session_data, config)

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

        # Right: screenshot thumbnails. Always built the same way regardless
        # of session_type — a transcript-only recording simply starts with
        # zero screenshot events (empty grid, handled below), same as any
        # full-mode session someone deleted every screenshot from. Photos can
        # be added to either via the Add Screenshot button.
        right = tk.Frame(pane, padx=8, pady=6)
        pane.add(right, minsize=260, width=500)

        head_row = tk.Frame(right)
        head_row.pack(fill="x")
        tk.Label(head_row, text="Screenshots", font=("TkDefaultFont", 10, "bold"), anchor="w").pack(side="left")

        tk.Button(
            head_row, text="Add Screenshot…", font=("TkDefaultFont", 8), command=self._import_screenshots
        ).pack(side="right", padx=(4, 0))

        # Render-mode selector: raw / blurred / circled, re-rendered live.
        tk.Label(head_row, text="Display:", fg="#6B7280", font=("TkDefaultFont", 8)).pack(
            side="right", padx=(8, 2)
        )
        self._mode_var = tk.StringVar(value=self._render_mode)
        self._mode_combo = ttk.Combobox(
            head_row,
            textvariable=self._mode_var,
            values=list(RENDER_MODES),
            state="readonly",
            width=9,
        )
        self._mode_combo.pack(side="right")
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)

        self._hint_var = tk.StringVar(value=self._compute_hint())
        if self._all_legacy:
            self._mode_combo.configure(state="disabled")
        tk.Label(
            right,
            textvariable=self._hint_var,
            fg="#6B7280",
            font=("TkDefaultFont", 8),
            anchor="w",
            wraplength=360,
            justify="left",
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
    # Render mode                                                          #
    # ------------------------------------------------------------------ #

    def _compute_hint(self) -> str:
        """Hint text under the Screenshots header — depends on actual
        screenshot state, never on session_type (see module docstring)."""
        if not self._screenshot_events:
            return (
                "No screenshots yet — narration alone will be sent for Documentation. "
                "Use “Add Screenshot…” to attach photos taken outside the app "
                "(e.g. BIOS setup steps AutoDoc can't screenshot directly)."
            )
        if self._all_legacy:
            return (
                "This session predates editable screenshot effects — its images "
                "were blurred permanently at capture, so the display mode cannot be changed."
            )
        return "Click Delete to remove a screenshot. Tip: remove the last few stop-click frames."

    def _on_mode_changed(self, _event=None) -> None:
        mode = self._mode_var.get()
        if mode == self._render_mode:
            return
        self._render_mode = mode
        self.session_data["screenshot_render_mode"] = mode
        self._rebuild_thumbnails()
        self._status_var.set(
            f"Display mode set to '{mode}' — click Save Changes to make it stick "
            f"for export and generation."
        )

    def _rebuild_thumbnails(self) -> None:
        """Destroy and re-create all thumbnails with the current render mode,
        preserving pending delete marks."""
        pending_deletes = set(self._deleted_ids)
        for frame in self._thumb_frames.values():
            frame.destroy()
        self._thumb_frames.clear()
        self._thumb_refs.clear()
        self._deleted_ids.clear()

        for evt in self._screenshot_events:
            if any(e["event_id"] == evt["event_id"] for e in self.session_data.get("events", [])):
                self._add_thumbnail(evt)

        # Re-apply delete marks to frames that still exist.
        for eid in pending_deletes:
            frame = self._thumb_frames.get(eid)
            if frame is not None:
                self._toggle_delete(eid, frame)

        self._hint_var.set(self._compute_hint())
        # _all_legacy can flip (e.g. importing legacy-style photos into a
        # session that previously had only real, non-legacy clicks), so
        # recompute the combo's enabled state every rebuild, not just at
        # construction time.
        self._mode_combo.configure(state="disabled" if self._all_legacy else "readonly")

    # ------------------------------------------------------------------ #
    # Add Screenshot (append externally-sourced photos)                    #
    # ------------------------------------------------------------------ #

    def _import_screenshots(self) -> None:
        """
        Let the user attach photos taken outside AutoDoc (a phone photo of a
        BIOS screen, for instance) to this session. Delegates the actual
        file-copy + event construction to recorder/session.py::
        import_screenshots() so the event schema lives in exactly one place.

        Files are copied to disk immediately on selection (consistent with
        how a live recording writes screenshots as they happen), but only
        become part of session.json when Save Changes is clicked — closing
        the editor without saving leaves the copies on disk but untracked,
        same tradeoff as every other unsaved edit in this window.
        """
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Add Screenshots",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return

        from recorder.session import import_screenshots

        try:
            self.session_data = import_screenshots(
                self.session_dir, self.session_data, [Path(p) for p in paths]
            )
        except OSError as exc:
            messagebox.showerror("AutoDoc", f"Could not import screenshot(s):\n{exc}", parent=self.root)
            return

        self._screenshot_events = sorted(
            [e for e in self.session_data.get("events", []) if e.get("type") == "screenshot"],
            key=lambda e: e["timestamp_offset_ms"],
        )
        self._all_legacy = bool(self._screenshot_events) and all(
            event_is_legacy(e) for e in self._screenshot_events
        )
        self._rebuild_thumbnails()
        self._status_var.set(
            f"Added {len(paths)} screenshot(s) — click Save Changes to keep them."
        )

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
        for evt in self._screenshot_events:
            self._add_thumbnail(evt)

        count = len(self._screenshot_events)
        self._status_var.set(f"{count} screenshot{'s' if count != 1 else ''} • Edit transcript then save or generate.")

    def _load_rendered(self, event: dict) -> Optional[Image.Image]:
        """Load an event's screenshot with the current render mode applied."""
        try:
            return render_event_image(self.session_dir, event, self._render_mode, self.config)
        except Exception:
            log.exception("editor: failed to render %s", event["data"].get("filename"))
            return None

    def _add_thumbnail(self, event: dict) -> None:
        img = self._load_rendered(event)
        if img is None:
            return

        img.thumbnail(
            (ui_service.scale(self.THUMB_W), ui_service.scale(self.THUMB_H)),
            Image.LANCZOS,
        )
        # master= keeps the PhotoImage bound to this window's interpreter.
        # With the single shared root there is only one interpreter anyway,
        # but being explicit costs nothing.
        photo = ImageTk.PhotoImage(img, master=self.root)
        self._thumb_refs.append(photo)

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
        img_label.bind("<Button-1>", lambda e, evt=event: self._show_full_image(evt))

        # Timestamp + click type label
        offset_s = event["timestamp_offset_ms"] / 1000
        click = event["data"].get("click_type", "left")
        click_label = {"manual": "manual capture", "imported": "imported"}.get(click, f"{click}-click")
        tk.Label(
            cell,
            text=f"{offset_s:.1f}s  •  {click_label}  •  click to expand",
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
        - Stores the chosen screenshot render mode.
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
        if not self._all_legacy:
            self.session_data["screenshot_render_mode"] = self._render_mode
        self._screenshot_events = [
            e for e in self._screenshot_events if e["event_id"] not in self._deleted_ids
        ]

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
            messagebox.showerror("AutoDoc", f"Could not save session:\n{exc}", parent=self.root)
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

    def _show_full_image(self, event: dict) -> None:
        """
        Full-size preview + redaction editor (PRD F3).

        Drag on the image to draw a privacy pixelation box. Regions are stored
        in image-space pixels on the event (data.redact_regions) and applied
        by the render pipeline in every mode — the raw PNG is never modified.
        Region edits mutate the shared event dict immediately (the same dict
        object lives in session_data["events"]), so the existing Save Changes
        button persists them to session.json.
        """
        img = self._load_rendered(event)
        if img is None:
            messagebox.showerror("AutoDoc", "Could not open image.", parent=self.root)
            return

        # Scale to fit within 1200×760 (at 96 DPI) without upscaling. ratio
        # maps image-space → display-space; divide to go back.
        max_w, max_h = ui_service.scale(1200), ui_service.scale(760)
        ratio = min(max_w / img.width, max_h / img.height, 1.0)
        disp_size = (int(img.width * ratio), int(img.height * ratio))

        popup = tk.Toplevel(self.root)
        popup.title(f"Screenshot — {event['data'].get('filename', '')} ({self._render_mode})")
        popup.resizable(False, False)

        tk.Label(
            popup,
            text="Drag on the image to pixelate sensitive areas (student data, IDs, emails). "
                 "Changes apply to previews, exports, and generated documents.",
            fg="#6B7280",
            font=("TkDefaultFont", 8),
            wraplength=disp_size[0],
            justify="left",
        ).pack(padx=8, pady=(8, 2), anchor="w")

        canvas = tk.Canvas(
            popup, width=disp_size[0], height=disp_size[1], highlightthickness=0, cursor="crosshair"
        )
        canvas.pack(padx=8, pady=2)

        state = {"start": None, "temp_rect": None, "dirty": False}

        def _redraw() -> None:
            fresh = self._load_rendered(event)
            if fresh is None:
                return
            disp = fresh.resize(disp_size, Image.LANCZOS)
            photo = ImageTk.PhotoImage(disp, master=popup)
            popup._photo = photo  # Prevent garbage collection.
            canvas.delete("all")
            canvas.create_image(0, 0, image=photo, anchor="nw")

        def _on_press(evt_tk) -> None:
            state["start"] = (evt_tk.x, evt_tk.y)

        def _on_drag(evt_tk) -> None:
            if state["start"] is None:
                return
            if state["temp_rect"] is not None:
                canvas.delete(state["temp_rect"])
            x0, y0 = state["start"]
            state["temp_rect"] = canvas.create_rectangle(
                x0, y0, evt_tk.x, evt_tk.y, outline="#DC2626", width=2
            )

        def _on_release(evt_tk) -> None:
            if state["start"] is None:
                return
            x0, y0 = state["start"]
            state["start"] = None
            if state["temp_rect"] is not None:
                canvas.delete(state["temp_rect"])
                state["temp_rect"] = None
            # Normalize, convert display → image space, ignore tiny boxes.
            left, top = min(x0, evt_tk.x), min(y0, evt_tk.y)
            w, h = abs(evt_tk.x - x0), abs(evt_tk.y - y0)
            if w < 5 or h < 5:
                return
            region = [
                int(left / ratio), int(top / ratio),
                int(w / ratio), int(h / ratio),
            ]
            event["data"].setdefault("redact_regions", []).append(region)
            state["dirty"] = True
            _redraw()

        canvas.bind("<ButtonPress-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_drag)
        canvas.bind("<ButtonRelease-1>", _on_release)

        def _undo_last() -> None:
            regions = event["data"].get("redact_regions") or []
            if regions:
                regions.pop()
                state["dirty"] = True
                _redraw()

        def _clear_all() -> None:
            if event["data"].get("redact_regions"):
                event["data"]["redact_regions"] = []
                state["dirty"] = True
                _redraw()

        def _done() -> None:
            popup.destroy()
            if state["dirty"]:
                self._rebuild_thumbnails()
                self._status_var.set(
                    "Redaction regions changed — click Save Changes to keep them."
                )

        btn_row = tk.Frame(popup)
        btn_row.pack(pady=(4, 8))
        tk.Button(btn_row, text="Undo Last Box", command=_undo_last, padx=10).pack(side="left", padx=4)
        tk.Button(btn_row, text="Clear All Boxes", command=_clear_all, fg="#DC2626", padx=10).pack(side="left", padx=4)
        tk.Button(btn_row, text="Done", command=_done, padx=16).pack(side="left", padx=4)
        popup.protocol("WM_DELETE_WINDOW", _done)

        _redraw()
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
        self._status_var.set(
            "Generating documentation in the background — the editor stays usable."
        )

        def _work():
            """Worker thread. Reports back via ui_service.call_soon ONLY."""
            try:
                from generation.generator import generate_documentation
                from generation.renderers import write_outputs
                from config import get_app_dir

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

    def _on_done(self, output_path: Path) -> None:
        self._status_var.set(f"Saved: {output_path.name}")
        if messagebox.askyesno(
            "AutoDoc — Done",
            f"Documentation saved to:\n{output_path}\n\nOpen now?",
            parent=self.root,
        ):
            import subprocess, sys
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(output_path)])
            else:
                subprocess.Popen(["open", str(output_path)])

    def _on_error(self, error: str) -> None:
        self._status_var.set("Generation failed.")
        messagebox.showerror(
            "AutoDoc — Error",
            f"Could not generate documentation:\n\n{error}",
            parent=self.root,
        )
