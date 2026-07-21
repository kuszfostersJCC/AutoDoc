"""
recorder/session.py — Session lifecycle manager.

A Session coordinates audio capture, mouse hooks, and screenshot capture for
one recording. It maintains a timestamped event log (session.json) and writes
it atomically after every screenshot so that a crash doesn't lose the session.

Status transitions:
    "recording"         — actively capturing (set on start())
    "transcribed"       — Whisper has run, segments merged (set on add_transcript_segments())
    "ready"             — fully finalised, stopped_at written (set on finalize())
    "transcript_failed" — finalised but transcription failed/timed out
                          (finalize(transcript_ok=False)); recoverable via the
                          Retranscribe button in the Sessions window. Before
                          2026-07-08 the failure path skipped finalize()
                          entirely, leaving sessions stuck at "recording"
                          forever with no retry — never regress that.

Race-condition note: Session owns self._status. Every flush uses that field
rather than a hardcoded string, so a late-finishing screenshot thread can never
overwrite a "ready" status back to "recording".
"""

import json
import shutil
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import AutoDocConfig, get_app_dir
from recorder.audio_capture import AudioCapture
from recorder.mouse_hooks import MouseHooks
from recorder.screenshot import capture_raw, save_screenshot_raw
from recorder.ui_inspect import get_click_target


class Session:
    def __init__(self, config: AutoDocConfig) -> None:
        self.config = config

        # Identity / timing
        self._start_mono: Optional[float] = None
        self._start_time: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._session_id: Optional[str] = None
        self._session_dir: Optional[Path] = None

        # Human-readable name (set by user after stopping)
        self._name: Optional[str] = None

        # Recording mode: "full" (auto-captures a screenshot on every click)
        # or "transcript_only" (clicks aren't auto-captured). This is the
        # ONLY thing the mode controls — manual (hotkey) capture and
        # imported photos work in both, and a "transcript_only" session is
        # not a guarantee of zero screenshots. See start()'s comment and
        # import_screenshots() below.
        self._mode: str = "full"

        # Event log
        self._events: list = []
        self._screenshot_count = 0

        # Status — single source of truth for session.json
        self._status = "idle"

        # Sub-components
        self._audio = AudioCapture()
        self._hooks: Optional[MouseHooks] = None

        # Threading
        self._lock = threading.Lock()
        self._running = False

        # Pause bookkeeping (PRD F4). INVARIANT: audio drops frames while
        # paused and event offsets subtract cumulative paused time, so both
        # timelines exclude pauses and Whisper timestamps stay aligned with
        # screenshot offsets. Any change here must preserve that.
        self._paused = False
        self._pause_started: Optional[float] = None
        self._paused_total = 0.0

        # Resume bookkeeping (PRD F5). When re-recording into an existing
        # session, new-event offsets start at the exact duration of the
        # pre-existing audio (frames/rate — no wall-clock drift), and the new
        # audio chunk is concatenated onto audio_full.wav on stop().
        self._resumed = False
        self._offset_base_ms = 0
        # Top-level session.json keys we don't own (e.g. screenshot_render_mode)
        # — preserved across flushes so a resume doesn't erase editor choices.
        self._extra_keys: dict = {}

    # ------------------------------------------------------------------ #
    # Public read-only properties                                          #
    # ------------------------------------------------------------------ #

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    @property
    def started_at_iso(self) -> Optional[str]:
        return self._start_time.isoformat() if self._start_time else None

    @property
    def name(self) -> Optional[str]:
        return self._name

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self, mode: str = "full", resume_dir: Optional[Path] = None) -> Path:
        """
        Begin audio + mouse capture.

        Fresh session (resume_dir=None): creates a new session folder.
        Resume (PRD F5): re-opens the existing session at resume_dir —
        screenshots and their edits are kept, old transcript segments are
        dropped (the full concatenated audio is retranscribed on stop), and
        new-event offsets continue exactly where the existing audio ends.
        """
        self._start_mono = time.monotonic()

        if resume_dir is not None:
            self._prepare_resume(Path(resume_dir))
            mode = self._mode  # resume always uses the session's own mode
        else:
            self._start_time = datetime.now(timezone.utc)
            self._session_id = self._start_time.strftime("%Y%m%d_%H%M%S")
            sessions_root = get_app_dir() / self.config.sessions_dir
            self._session_dir = sessions_root / self._session_id
            self._session_dir.mkdir(parents=True, exist_ok=True)
            (self._session_dir / "screenshots").mkdir(exist_ok=True)
            self._events = []
            self._screenshot_count = 0
            self._mode = mode

        self._status = "recording"
        self._running = True

        self._flush_json()

        # Resumed sessions record the new chunk separately; stop() splices it
        # onto audio_full.wav (the wave module cannot append in place).
        wav_name = "audio_new.wav" if self._resumed else "audio_full.wav"
        try:
            self._audio.start(self._session_dir / wav_name)
        except Exception as exc:
            raise RuntimeError(
                f"Could not open microphone for recording.\n"
                f"Check that a microphone is connected and that Windows has "
                f"permission to access it (Settings → Privacy → Microphone).\n"
                f"Detail: {exc}"
            ) from exc

        # The ONE place self._mode legitimately gates behavior: whether clicks
        # are auto-captured. Everything else (UI panels, export templates,
        # generation) should branch on whether the session actually HAS
        # screenshot events, never on this mode string — see
        # recorder/session.py::import_screenshots() and HANDOFF.md.
        if mode == "full":
            self._hooks = MouseHooks(
                on_left_click=lambda x, y: self._handle_click(x, y, "left"),
                on_right_click=lambda x, y: self._handle_click(x, y, "right"),
            )
            self._hooks.start()

        return self._session_dir

    _OWNED_KEYS = (
        "schema", "session_id", "name", "session_type", "started_at",
        "stopped_at", "status", "events", "edited_full_transcript",
    )

    def _prepare_resume(self, resume_dir: Path) -> None:
        """Load existing session state for append-recording."""
        with open(resume_dir / "session.json", encoding="utf-8") as f:
            data = json.load(f)

        wav_path = resume_dir / "audio_full.wav"
        if not wav_path.exists():
            raise RuntimeError(
                "This session has no audio_full.wav, so it cannot be resumed."
            )
        with wave.open(str(wav_path), "rb") as wf:
            self._offset_base_ms = int(wf.getnframes() * 1000 / wf.getframerate())

        self._resumed = True
        self._session_dir = resume_dir
        self._session_id = data.get("session_id", resume_dir.name)
        self._name = data.get("name")
        self._mode = data.get("session_type", "full")
        # Original start time preserved so total duration keeps growing.
        started = data.get("started_at")
        self._start_time = datetime.fromisoformat(started) if started else datetime.now(timezone.utc)

        # Old transcript segments are invalid the moment new audio exists —
        # the whole concatenated file is retranscribed on stop. A stale
        # edited_full_transcript would silently win at generation time, so it
        # is dropped too (the Record More dialog warns about this).
        self._events = [
            e for e in data.get("events", []) if e.get("type") != "transcript_segment"
        ]
        self._extra_keys = {k: v for k, v in data.items() if k not in self._OWNED_KEYS}

        counts = [
            int(e["event_id"][4:])
            for e in self._events
            if e.get("type") == "screenshot" and e.get("event_id", "").startswith("evt_")
        ]
        self._screenshot_count = max(counts, default=0)
        (self._session_dir / "screenshots").mkdir(exist_ok=True)

    def _splice_resumed_audio(self) -> None:
        """Concatenate audio_new.wav onto audio_full.wav (same format by
        construction: 16 kHz mono int16). Runs after capture stops."""
        full_path = self._session_dir / "audio_full.wav"
        new_path = self._session_dir / "audio_new.wav"
        if not new_path.exists():
            return
        with wave.open(str(full_path), "rb") as wf:
            params = wf.getparams()
            frames_old = wf.readframes(wf.getnframes())
        with wave.open(str(new_path), "rb") as wf:
            frames_new = wf.readframes(wf.getnframes())

        tmp_path = self._session_dir / "audio_full.wav.tmp"
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setparams(params)
            wf.writeframes(frames_old)
            wf.writeframes(frames_new)
        tmp_path.replace(full_path)
        new_path.unlink(missing_ok=True)

    def stop(self) -> Path:
        """Stop capture. Returns session directory for the caller to use."""
        if self._paused:
            self.resume()  # close out pause bookkeeping before stopping
        self._running = False
        if self._hooks:
            self._hooks.stop()
            self._hooks = None
        self._audio.stop()
        if self._resumed:
            self._splice_resumed_audio()
        return self._session_dir

    # ------------------------------------------------------------------ #
    # Pause / resume / manual capture (PRD F4)                             #
    # ------------------------------------------------------------------ #

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Suspend capture: clicks ignored, audio frames dropped."""
        if not self._running or self._paused:
            return
        self._paused = True
        self._pause_started = time.monotonic()
        self._audio.set_paused(True)

    def resume(self) -> None:
        """Resume capture; paused span excluded from both timelines."""
        if not self._paused:
            return
        if self._pause_started is not None:
            self._paused_total += time.monotonic() - self._pause_started
        self._pause_started = None
        self._paused = False
        self._audio.set_paused(False)

    def manual_capture(self) -> None:
        """
        Hotkey-triggered screenshot at the current cursor position, without a
        click (result screens, open menus). click_type "manual" flows through
        the normal pipeline; content.py describes it as a manual capture.

        Deliberately NOT gated on self._mode: auto-capture-on-click and
        manual (hotkey) capture are independent concerns. "transcript_only"
        only ever meant "don't auto-capture clicks" — it never meant "no
        screenshots are possible," so the hotkey works in either mode.
        """
        if not self._running or self._paused:
            return
        from pynput.mouse import Controller

        x, y = Controller().position
        self._handle_click(int(x), int(y), "manual")

    def _now_offset_ms(self) -> int:
        """Event offset: wall time since capture start MINUS cumulative paused
        time, PLUS the resumed-session base (duration of pre-existing audio).
        Aligns with the concatenated WAV by construction."""
        paused = self._paused_total
        if self._paused and self._pause_started is not None:
            paused += time.monotonic() - self._pause_started
        return self._offset_base_ms + int(
            (time.monotonic() - self._start_mono - paused) * 1000
        )

    def set_name(self, name: str) -> None:
        """Set a human-readable name for this session and persist it immediately."""
        with self._lock:
            self._name = name.strip() or None
            if self._session_dir:
                self._flush_json()

    def add_transcript_segments(self, segments: list) -> None:
        """
        Install Whisper transcript segments into the event log, REPLACING any
        existing ones. Replacement (not append) matters for resumed sessions,
        where the whole concatenated audio is retranscribed; for fresh
        sessions there is nothing to replace and this is a plain extend.
        """
        with self._lock:
            self._events = [
                e for e in self._events if e.get("type") != "transcript_segment"
            ]
            self._events.extend(segments)
            self._status = "transcribed"
            self._flush_json()

    def finalize(self, stopped_at: datetime, transcript_ok: bool = True) -> None:
        """
        Record the stop time and write the final JSON.

        transcript_ok=False marks the session "transcript_failed" instead of
        "ready" — the session (audio, screenshots) is fully saved and the
        Sessions window offers Retranscribe. ALWAYS call finalize, even when
        transcription fails: skipping it leaves status stuck at "recording".
        """
        with self._lock:
            self._stopped_at = stopped_at
            self._status = "ready" if transcript_ok else "transcript_failed"
            self._flush_json()

    # ------------------------------------------------------------------ #
    # Click → screenshot pipeline                                          #
    # ------------------------------------------------------------------ #

    def _handle_click(self, x: int, y: int, click_type: str) -> None:
        if not self._running or self._paused:
            return

        with self._lock:
            if self._screenshot_count >= self.config.max_screenshots_per_session:
                return
            self._screenshot_count += 1
            count = self._screenshot_count

        offset_ms = self._now_offset_ms()
        filename = f"screenshots/{count:04d}_{offset_ms}.png"
        dest_path = self._session_dir / filename

        threading.Thread(
            target=self._capture_and_save,
            args=(x, y, click_type, offset_ms, count, filename, dest_path),
            daemon=True,
        ).start()

    def _capture_and_save(
        self,
        x: int,
        y: int,
        click_type: str,
        offset_ms: int,
        count: int,
        filename: str,
        dest_path: Path,
    ) -> None:
        # Identify the clicked element FIRST — the UI may react to the click
        # (menus close, dialogs open), so every millisecond between click and
        # inspection loses fidelity. Best-effort: None is fine (PRD F1).
        ui_element = None
        try:
            ui_element = get_click_target(x, y)
        except Exception:
            pass  # ui_inspect already logs; a click must never fail over this.

        try:
            # Stored RAW — blur/circle effects are applied at render time
            # (editor/export/generation) so they stay editable per-session.
            # See recorder/screenshot.py module docstring.
            img, img_x, img_y = capture_raw(x, y)
            click_img_x, click_img_y = save_screenshot_raw(
                img, img_x, img_y, dest_path, self.config.max_image_dimension
            )
        except Exception:
            return

        event = {
            "event_id": f"evt_{count:04d}",
            "type": "screenshot",
            "timestamp_offset_ms": offset_ms,
            "data": {
                "filename": filename,
                "click_type": click_type,
                "click_x": x,
                "click_y": y,
                # Click position in the SAVED image's pixel space (post-
                # downscale) — required to re-render blur/circle effects.
                # Absence of these keys marks a legacy pre-raw-storage event.
                "click_img_x": click_img_x,
                "click_img_y": click_img_y,
            },
            "edited_text": None,
        }
        if ui_element:
            # Ground truth for the generator: what was actually clicked
            # (accessible name/role) and where (window title, app).
            event["data"]["ui_element"] = ui_element

        with self._lock:
            self._events.append(event)
            # self._status is always used — no hardcoded "recording" here,
            # so a late thread can't overwrite a "ready" status.
            self._flush_json()

    # ------------------------------------------------------------------ #
    # JSON persistence                                                     #
    # ------------------------------------------------------------------ #

    def _flush_json(self) -> None:
        """Serialize current state to session.json atomically. Caller holds lock."""
        data = {
            # Keys we don't own (editor's screenshot_render_mode etc.) first,
            # so our owned keys below always win on collision.
            **self._extra_keys,
            "schema": "autodoc-session/v1",
            "session_id": self._session_id,
            "name": self._name,
            "session_type": self._mode,
            "started_at": self._start_time.isoformat() if self._start_time else None,
            "stopped_at": self._stopped_at.isoformat() if self._stopped_at else None,
            "status": self._status,
            "events": sorted(self._events, key=lambda e: e["timestamp_offset_ms"]),
        }
        path = self._session_dir / "session.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)


# ---------------------------------------------------------------------- #
# Post-hoc screenshot import (append to an already-saved session)          #
# ---------------------------------------------------------------------- #

def import_screenshots(session_dir: Path, session_data: dict, image_paths: list) -> dict:
    """
    Append externally-sourced images (e.g. phone photos of a BIOS screen that
    AutoDoc physically cannot screenshot) to an already-saved session as
    screenshot events. This is a free function, not a Session method: it
    operates on a session.json dict + folder on disk, not a live recording,
    the same way the Sessions window's Retranscribe already does.

    These events deliberately have NO click_x/click_y/click_img_x/click_img_y
    — there is no real click behind a photograph. Missing click_img_x/
    click_img_y is exactly what recorder/screenshot.py::event_is_legacy()
    checks for, so these render as plain raw images (no blur/circle math
    attempted — there's no click point to center it on) while still
    supporting manual redaction, which only depends on data.redact_regions.
    click_type="imported" is a new value content.py's _describe_click() and
    the session editor's thumbnail label special-case, so the model isn't
    told a fictional click happened.

    Placement: each image is appended 1 second after the previous highest
    timestamp_offset_ms in the session, so imported photos always sort after
    every existing event. This is deliberately coarse. If the session has an
    edited_full_transcript, content.py places ALL screenshots after the full
    narration block in chronological order regardless of exact spacing — so
    only the relative order among imported photos matters, not alignment
    with a specific narration segment. That's the intended pairing: import
    photos, then review/finalize the transcript as one block in the editor.

    Args:
        session_dir:   The session's folder (screenshots/ created if absent).
        session_data:  Parsed session.json dict — events are appended in
                       place and the same dict is returned for convenience.
        image_paths:   Source image file paths (any format Pillow can open —
                       does not need to be PNG).

    Returns:
        The same session_data dict, mutated. Caller is responsible for
        persisting it (e.g. the session editor's existing Save Changes path).
    """
    events = session_data.setdefault("events", [])

    counts = [
        int(e["event_id"][4:])
        for e in events
        if e.get("type") == "screenshot" and e.get("event_id", "").startswith("evt_")
    ]
    next_count = max(counts, default=0)
    next_offset = max((e.get("timestamp_offset_ms", 0) for e in events), default=0)

    dest_dir = session_dir / "screenshots"
    dest_dir.mkdir(exist_ok=True)

    for src in image_paths:
        src = Path(src)
        next_count += 1
        next_offset += 1000  # 1s apart — keeps sort order unambiguous
        ext = src.suffix.lower() or ".png"
        filename = f"screenshots/{next_count:04d}_{next_offset}{ext}"
        shutil.copy2(src, session_dir / filename)

        events.append({
            "event_id": f"evt_{next_count:04d}",
            "type": "screenshot",
            "timestamp_offset_ms": next_offset,
            "data": {
                "filename": filename,
                "click_type": "imported",
            },
            "edited_text": None,
        })

    session_data["events"] = sorted(events, key=lambda e: e["timestamp_offset_ms"])
    return session_data
