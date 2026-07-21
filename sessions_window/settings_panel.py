"""
sessions_window/settings_panel.py — The Settings tab inside the Sessions window.

An interactive editor for the config.json values that plausibly change
semi-regularly: recording hotkeys, the default screenshot display mode,
documentation output formats, the AI backend, and the Whisper model.
Deliberately NOT exposed here: api_key (sensitive — config.json only),
directory paths and capture limits (IT-install decisions, not user settings).

Persistence model: this panel mutates the SAME AutoDocConfig instance the
whole app holds (TrayApp, Session, generation all share it), then persists
via config.write_config(). Consequences, stated per field in the UI:
  - screenshot mode / output formats / backend: take effect immediately,
  - hotkeys: read when a recording starts → apply to the NEXT recording,
  - whisper model: loaded once at startup → requires an app restart.

Runs on the ui_service UI thread like everything else in this window.
"""

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

import ui_service
from config import AutoDocConfig, write_config
from recorder.screenshot import RENDER_MODES
from version import VERSION

log = logging.getLogger(__name__)

_FORMAT_ORDER = ("docx", "html", "md")
_FORMAT_LABELS = {"docx": "Word (.docx)", "html": "Web page (.html)", "md": "Markdown (.md)"}
_WHISPER_MODELS = ("tiny", "base", "small")
_PROVIDERS = ("anthropic", "local")


class SettingsPanel(tk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        config: AutoDocConfig,
        is_idle: Optional[Callable[[], bool]] = None,
        quit_for_update: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.config_obj = config
        self._is_idle = is_idle
        self._quit_for_update = quit_for_update
        self._dirty = False
        self._pending_update = None  # updater.UpdateInfo once a check finds one
        self._build_scroll_container()
        self._build()
        self._track_changes()

    def _build_scroll_container(self) -> None:
        """
        All content lives inside a scrollable canvas so nothing is ever
        clipped on short windows or unusual DPI scales (reported 2026-07-14).
        self._content is the frame every section packs into.
        """
        self._canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._content = tk.Frame(self._canvas, padx=12, pady=10)
        content_window = self._canvas.create_window((0, 0), window=self._content, anchor="nw")

        self._content.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        # Keep the inner frame as wide as the canvas.
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(content_window, width=e.width),
        )
        # Mousewheel only while the pointer is over this tab (bind_all is
        # global per interpreter — scope it via Enter/Leave, and unbind on
        # destroy so a closed window doesn't leave a dangling binding).
        self._canvas.bind("<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self._canvas.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event: tk.Event) -> None:
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _track_changes(self) -> None:
        """Flag unsaved edits — 'did I forget to save?' should never be a
        mystery (field report 2026-07-14: unchecked a format, still got the
        file, couldn't tell whether save had happened)."""
        variables = [
            self._stop_var, self._pause_var, self._manual_var, self._mode_var,
            self._provider_var, self._endpoint_var, self._local_model_var,
            self._whisper_var, *self._format_vars.values(),
        ]
        for var in variables:
            var.trace_add("write", self._mark_dirty)

    def _mark_dirty(self, *_args) -> None:
        self._dirty = True
        self._status_var.set("Unsaved changes — click Save Settings to apply.")

    # ------------------------------------------------------------------ #
    # UI                                                                   #
    # ------------------------------------------------------------------ #

    def _grid_row(self, frame: tk.Widget, row: int, label: str, widget: tk.Widget, hint: str = "") -> None:
        tk.Label(frame, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        widget.grid(row=row, column=1, sticky="w", pady=3)
        if hint:
            tk.Label(frame, text=hint, fg="#6B7280", font=("TkDefaultFont", 8), anchor="w").grid(
                row=row, column=2, sticky="w", padx=(10, 0)
            )

    def _build(self) -> None:
        cfg = self.config_obj

        # ── Keyboard shortcuts ────────────────────────────────────── #
        keys = tk.LabelFrame(self._content, text="Keyboard Shortcuts (while recording)", padx=10, pady=8)
        keys.pack(fill="x", pady=(0, 10))

        self._stop_var = tk.StringVar(value=cfg.stop_hotkey)
        self._pause_var = tk.StringVar(value=cfg.pause_hotkey)
        self._manual_var = tk.StringVar(value=cfg.manual_capture_hotkey)

        self._grid_row(keys, 0, "Stop recording:", tk.Entry(keys, textvariable=self._stop_var, width=22),
                       "Ends the recording without any tray clicks being captured.")
        self._grid_row(keys, 1, "Pause / resume:", tk.Entry(keys, textvariable=self._pause_var, width=22),
                       "Suspends clicks and narration until pressed again.")
        self._grid_row(keys, 2, "Manual screenshot:", tk.Entry(keys, textvariable=self._manual_var, width=22),
                       "Captures the screen without clicking (result pages, open menus).")
        tk.Label(
            keys,
            text="Format: <ctrl>+<shift>+s — leave blank to disable. Changes apply to the next recording.",
            fg="#6B7280", font=("TkDefaultFont", 8), anchor="w",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ── Screenshots ──────────────────────────────────────────── #
        shots = tk.LabelFrame(self._content, text="Screenshots", padx=10, pady=8)
        shots.pack(fill="x", pady=(0, 10))

        self._mode_var = tk.StringVar(value=cfg.screenshot_mode)
        mode_combo = ttk.Combobox(
            shots, textvariable=self._mode_var, values=list(RENDER_MODES),
            state="readonly", width=12,
        )
        self._grid_row(shots, 0, "Default display mode:", mode_combo,
                       "raw = untouched • blurred = privacy blur • circled = red circle at click. "
                       "Each session can override this in the editor.")

        # ── Documentation output ─────────────────────────────────── #
        out = tk.LabelFrame(self._content, text="Documentation Output", padx=10, pady=8)
        out.pack(fill="x", pady=(0, 10))

        self._format_vars: dict[str, tk.BooleanVar] = {}
        fmt_row = tk.Frame(out)
        fmt_row.grid(row=0, column=0, sticky="w")
        for fmt in _FORMAT_ORDER:
            var = tk.BooleanVar(value=fmt in (cfg.output_formats or []))
            self._format_vars[fmt] = var
            tk.Checkbutton(fmt_row, text=_FORMAT_LABELS[fmt], variable=var).pack(side="left", padx=(0, 12))
        tk.Label(
            out, text="Formats written when generating documentation. At least one required.",
            fg="#6B7280", font=("TkDefaultFont", 8), anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        # ── AI backend ───────────────────────────────────────────── #
        ai = tk.LabelFrame(self._content, text="AI Backend", padx=10, pady=8)
        ai.pack(fill="x", pady=(0, 10))

        self._provider_var = tk.StringVar(value=cfg.provider)
        provider_combo = ttk.Combobox(
            ai, textvariable=self._provider_var, values=list(_PROVIDERS),
            state="readonly", width=12,
        )
        self._grid_row(ai, 0, "Provider:", provider_combo,
                       "anthropic = Claude API (needs api_key in config.json) • local = your own vision model.")

        self._endpoint_var = tk.StringVar(value=cfg.local_endpoint)
        self._grid_row(ai, 1, "Local endpoint:", tk.Entry(ai, textvariable=self._endpoint_var, width=44),
                       "OpenAI-compatible chat endpoint (Ollama, LM Studio…). Used when provider = local.")

        self._local_model_var = tk.StringVar(value=cfg.local_model)
        model_row = tk.Frame(ai)
        # Editable combobox: type a name, or Fetch to list what the server
        # actually serves (/v1/models) — ids must match exactly (llama.cpp
        # derives them from GGUF filenames; Ollama uses registry tags).
        self._model_combo = ttk.Combobox(
            model_row, textvariable=self._local_model_var, width=28, values=[]
        )
        self._model_combo.pack(side="left")
        self._fetch_btn = tk.Button(
            model_row, text="Fetch", padx=8, command=self._fetch_models
        )
        self._fetch_btn.pack(side="left", padx=(4, 0))
        self._grid_row(ai, 2, "Local model:", model_row,
                       "Must match a model id on the endpoint. Fetch queries the server's /v1/models.")

        self._whisper_var = tk.StringVar(value=cfg.whisper_model)
        whisper_combo = ttk.Combobox(
            ai, textvariable=self._whisper_var, values=list(_WHISPER_MODELS),
            state="readonly", width=12,
        )
        self._grid_row(ai, 3, "Speech model:", whisper_combo,
                       "Whisper size — bigger is more accurate but slower. Takes effect after restarting AutoDoc.")

        # ── Updates ──────────────────────────────────────────────── #
        upd = tk.LabelFrame(self._content, text="Updates", padx=10, pady=8)
        upd.pack(fill="x", pady=(0, 10))

        tk.Label(upd, text=f"Current version: {VERSION}", anchor="w").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self._update_btn = tk.Button(
            upd, text="Check for Updates", command=self._check_for_updates
        )
        self._update_btn.grid(row=1, column=0, sticky="w")
        self._update_status_var = tk.StringVar(value="")
        tk.Label(
            upd, textvariable=self._update_status_var, fg="#6B7280",
            font=("TkDefaultFont", 8), anchor="w", wraplength=520, justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        # ── Save row ─────────────────────────────────────────────── #
        save_row = tk.Frame(self._content)
        save_row.pack(fill="x", pady=(4, 0))
        self._status_var = tk.StringVar(value="")
        tk.Label(save_row, textvariable=self._status_var, fg="#6B7280",
                 font=("TkDefaultFont", 8)).pack(side="left")
        tk.Button(
            save_row, text="Save Settings",
            bg="#2563EB", fg="white", activebackground="#1D4ED8", activeforeground="white",
            padx=14, command=self._save,
        ).pack(side="right")

    # ------------------------------------------------------------------ #
    # Model list fetch                                                     #
    # ------------------------------------------------------------------ #

    def _fetch_models(self) -> None:
        """Query the local endpoint's /v1/models in a worker thread and fill
        the model dropdown. Also warns about text-only models — a model
        without vision support silently ignores every screenshot."""
        endpoint = self._endpoint_var.get().strip()
        if not endpoint:
            self._status_var.set("Enter the local endpoint first, then Fetch.")
            return
        self._fetch_btn.config(state="disabled", text="…")
        self._status_var.set("Querying the model server…")

        def _work() -> None:
            try:
                from generation.backends.local_vision_backend import list_models

                models = list_models(endpoint)
                ui_service.call_soon(lambda: self._on_models(models))
            except Exception as exc:
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_models_error(msg))

        threading.Thread(target=_work, name="model-list-fetch", daemon=True).start()

    def _on_models(self, models: list) -> None:
        self._fetch_btn.config(state="normal", text="Fetch")
        if not models:
            self._status_var.set("The server responded but listed no models.")
            return
        ids = [m["id"] for m in models]
        self._model_combo.configure(values=ids)
        text_only = [m["id"] for m in models if m["text_only"]]
        msg = f"{len(ids)} model(s) found — pick one from the dropdown."
        if text_only:
            msg += (
                f" WARNING: text-only (cannot see screenshots): "
                f"{', '.join(text_only)}"
            )
        self._status_var.set(msg)

    def _on_models_error(self, error: str) -> None:
        self._fetch_btn.config(state="normal", text="Fetch")
        self._status_var.set(f"Model lookup failed — {error}")

    # ------------------------------------------------------------------ #
    # Save                                                                 #
    # ------------------------------------------------------------------ #

    def _validate_hotkeys(self) -> list[str]:
        """Return error strings for invalid/duplicate hotkey combos."""
        errors: list[str] = []
        combos = {
            "Stop recording": self._stop_var.get().strip(),
            "Pause / resume": self._pause_var.get().strip(),
            "Manual screenshot": self._manual_var.get().strip(),
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

        formats = [f for f in _FORMAT_ORDER if self._format_vars[f].get()]
        if not formats:
            errors.append("Select at least one documentation output format.")

        provider = self._provider_var.get().strip().lower()
        endpoint = self._endpoint_var.get().strip()
        if provider == "local" and not endpoint:
            errors.append("Provider 'local' needs a local endpoint URL.")

        if errors:
            self._status_var.set("Not saved — " + " • ".join(errors))
            return

        cfg = self.config_obj
        cfg.stop_hotkey = self._stop_var.get().strip()
        cfg.pause_hotkey = self._pause_var.get().strip()
        cfg.manual_capture_hotkey = self._manual_var.get().strip()
        cfg.screenshot_mode = self._mode_var.get()
        cfg.output_formats = formats
        cfg.provider = provider
        cfg.local_endpoint = endpoint
        cfg.local_model = self._local_model_var.get().strip()
        restart_needed = self._whisper_var.get() != cfg.whisper_model
        cfg.whisper_model = self._whisper_var.get()

        try:
            write_config(cfg)
        except Exception as exc:
            log.exception("settings save failed")
            self._status_var.set(f"Could not write config.json: {exc}")
            return

        msg = "Settings saved."
        if restart_needed:
            msg += " Restart AutoDoc to load the new speech model."
        self._dirty = False
        # Set AFTER the trace fires from nothing (traces only fire on var
        # writes, and none happen here — safe to overwrite the dirty text).
        self._status_var.set(msg)
        log.info("settings saved via Settings tab")

    # ------------------------------------------------------------------ #
    # Updates — manual only, via GitHub Releases (see updater.py)          #
    # ------------------------------------------------------------------ #

    def _check_for_updates(self) -> None:
        self._update_btn.config(state="disabled", text="Checking…")
        self._update_status_var.set("Checking GitHub for a newer release…")

        def _work() -> None:
            import updater

            try:
                info = updater.check_for_update()
                ui_service.call_soon(lambda: self._on_check_result(info))
            except updater.UpdateCheckError as exc:
                msg = str(exc)
                ui_service.call_soon(lambda: self._on_check_error(msg))

        threading.Thread(target=_work, name="update-check", daemon=True).start()

    def _on_check_result(self, info) -> None:
        self._update_btn.config(state="normal", text="Check for Updates")
        if info is None:
            self._update_status_var.set(f"You're on the latest version ({VERSION}).")
            return
        self._pending_update = info
        self._update_status_var.set(
            f"Version {info.version} is available (current: {VERSION})."
        )
        self._show_update_dialog(info)

    def _on_check_error(self, error: str) -> None:
        self._update_btn.config(state="normal", text="Check for Updates")
        self._update_status_var.set(error)

    def _show_update_dialog(self, info) -> None:
        """
        Release-notes dialog with Update Now / Not Now — this is the whole
        "let people decide if they need the new features" requirement: the
        release body IS the changelog shown here, nothing separate to write.
        """
        parent = self.winfo_toplevel()
        dialog = tk.Toplevel(parent)
        dialog.title("AutoDoc — Update Available")
        dialog.transient(parent)
        dialog.geometry(ui_service.scaled_geometry(480, 360))
        dialog.grab_set()

        size_mb = info.asset_size / (1024 * 1024) if info.asset_size else 0
        header = f"Version {info.version} is available (you have {VERSION})."
        if size_mb:
            header += f" ~{size_mb:.0f} MB download."
        tk.Label(dialog, text=header, anchor="w", justify="left", wraplength=440,
                 font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=12, pady=(12, 6))

        notes_frame = tk.Frame(dialog)
        notes_frame.pack(fill="both", expand=True, padx=12)
        notes_text = tk.Text(notes_frame, wrap="word", height=10, relief="solid", borderwidth=1)
        notes_scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=notes_text.yview)
        notes_text.configure(yscrollcommand=notes_scroll.set)
        notes_text.insert("1.0", info.notes)
        notes_text.configure(state="disabled")
        notes_text.pack(side="left", fill="both", expand=True)
        notes_scroll.pack(side="right", fill="y")

        btn_row = tk.Frame(dialog)
        btn_row.pack(fill="x", padx=12, pady=12)
        tk.Button(btn_row, text="Not Now", command=dialog.destroy).pack(side="right")
        tk.Button(
            btn_row, text="Update Now", bg="#2563EB", fg="white",
            activebackground="#1D4ED8", activeforeground="white", padx=10,
            command=lambda: self._start_update(dialog, info),
        ).pack(side="right", padx=(0, 8))

    def _start_update(self, dialog: tk.Toplevel, info) -> None:
        if self._is_idle is not None and not self._is_idle():
            messagebox.showerror(
                "AutoDoc — Update",
                "Finish or stop the current recording before updating.\n\n"
                "Applying an update requires closing AutoDoc, which would "
                "interrupt a recording in progress.",
                parent=dialog,
            )
            return

        dialog.destroy()
        self._update_btn.config(state="disabled", text="Downloading…")
        self._update_status_var.set(f"Downloading version {info.version}…")

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
        Spawn the detached updater script, then quit AutoDoc immediately —
        see updater.py's module docstring for why the file swap can't happen
        in-process. Nothing after quit_for_update() runs; the relaunch
        happens from the script, not from this process.
        """
        import updater

        self._update_status_var.set("Restarting to apply the update…")
        updater.launch_update_and_exit(zip_path)
        if self._quit_for_update is not None:
            self._quit_for_update()
