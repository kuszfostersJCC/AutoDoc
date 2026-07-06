"""
recorder/session.py — Session lifecycle manager.

A Session coordinates audio capture, mouse hooks, and screenshot capture for
one recording. It maintains a timestamped event log (session.json) and writes
it atomically after every screenshot so that a crash doesn't lose the session.

Status transitions:
    "recording"   — actively capturing (set on start())
    "transcribed" — Whisper has run, segments merged (set on add_transcript_segments())
    "ready"       — fully finalised, stopped_at written (set on finalize())

Race-condition note: Session owns self._status. Every flush uses that field
rather than a hardcoded string, so a late-finishing screenshot thread can never
overwrite a "ready" status back to "recording".
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import AutoDocConfig, get_app_dir
from recorder.audio_capture import AudioCapture
from recorder.mouse_hooks import MouseHooks
from recorder.screenshot import capture_and_blur, save_screenshot


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

        # Recording mode: "full" (audio + screenshots) or "transcript_only" (audio only)
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

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self, mode: str = "full") -> Path:
        """Create the session folder and begin audio + mouse capture."""
        self._start_mono = time.monotonic()
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

        wav_path = self._session_dir / "audio_full.wav"
        try:
            self._audio.start(wav_path)
        except Exception as exc:
            raise RuntimeError(
                f"Could not open microphone for recording.\n"
                f"Check that a microphone is connected and that Windows has "
                f"permission to access it (Settings → Privacy → Microphone).\n"
                f"Detail: {exc}"
            ) from exc

        if mode == "full":
            self._hooks = MouseHooks(
                on_left_click=lambda x, y: self._handle_click(x, y, "left"),
                on_right_click=lambda x, y: self._handle_click(x, y, "right"),
            )
            self._hooks.start()

        return self._session_dir

    def stop(self) -> Path:
        """Stop capture. Returns session directory for the caller to use."""
        self._running = False
        if self._hooks:
            self._hooks.stop()
            self._hooks = None
        self._audio.stop()
        return self._session_dir

    def set_name(self, name: str) -> None:
        """Set a human-readable name for this session and persist it immediately."""
        with self._lock:
            self._name = name.strip() or None
            if self._session_dir:
                self._flush_json()

    def add_transcript_segments(self, segments: list) -> None:
        """Merge Whisper transcript segments into the event log."""
        with self._lock:
            self._events.extend(segments)
            self._status = "transcribed"
            self._flush_json()

    def finalize(self, stopped_at: datetime) -> None:
        """Record the stop time, mark as ready, and write the final JSON."""
        with self._lock:
            self._stopped_at = stopped_at
            self._status = "ready"
            self._flush_json()

    # ------------------------------------------------------------------ #
    # Click → screenshot pipeline                                          #
    # ------------------------------------------------------------------ #

    def _handle_click(self, x: int, y: int, click_type: str) -> None:
        if not self._running:
            return

        with self._lock:
            if self._screenshot_count >= self.config.max_screenshots_per_session:
                return
            self._screenshot_count += 1
            count = self._screenshot_count

        offset_ms = int((time.monotonic() - self._start_mono) * 1000)
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
        try:
            img = capture_and_blur(
                x, y,
                self.config.focus_radius_px,
                self.config.blur_strength,
            )
            save_screenshot(img, dest_path, self.config.max_image_dimension)
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
            },
            "edited_text": None,
        }

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
