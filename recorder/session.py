"""Session lifecycle: audio, mouse hooks and screenshots for one recording, plus
the session.json event log (rewritten atomically after every screenshot).
"""

import json
import logging
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

log = logging.getLogger(__name__)


class Session:
    def __init__(self, config: AutoDocConfig) -> None:
        self.config = config

        self._start_mono: Optional[float] = None
        self._start_time: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._session_id: Optional[str] = None
        self._session_dir: Optional[Path] = None

        self._name: Optional[str] = None

        # "full" auto-captures a screenshot on every click; "transcript_only" does
        # not. Neither is a guarantee about how many screenshots a session has.
        self._mode: str = "full"

        self._events: list = []
        self._screenshot_count = 0

        self._status = "idle"

        self._audio = AudioCapture()
        self._hooks: Optional[MouseHooks] = None

        self._lock = threading.Lock()
        self._running = False

        self._paused = False
        self._pause_started: Optional[float] = None
        self._paused_total = 0.0

        # Resume: new-event offsets start at the duration of the pre-existing audio.
        self._resumed = False
        self._offset_base_ms = 0
        self._extra_keys: dict = {}

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

    # ---- Lifecycle ----

    def start(self, mode: str = "full", resume_dir: Optional[Path] = None) -> Path:
        """Begin audio + mouse capture in a new session folder, or in resume_dir:
        screenshots kept, old segments dropped, offsets continuing at the audio end."""
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

        # Resumed runs record separately; stop() splices (wave can't append in place).
        wav_name = "audio_new.wav" if self._resumed else "audio_full.wav"
        try:
            self._audio.start(
                self._session_dir / wav_name,
                device_name=getattr(self.config, "audio_device", "") or "",
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not open microphone for recording.\n"
                f"Check that a microphone is connected and that Windows has "
                f"permission to access it (Settings → Privacy → Microphone).\n"
                f"If you picked a specific device in AutoDoc's Settings → "
                f"Microphone, try System default instead.\n"
                f"Detail: {exc}"
            ) from exc

        # The one place _mode gates behaviour; elsewhere test for screenshot events.
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

        # New audio invalidates old segments and any edited_full_transcript, which
        # _OWNED_KEYS drops; the whole concatenated file is retranscribed on stop.
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
        """Concatenate audio_new.wav onto audio_full.wav (16 kHz mono int16 both)."""
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
        """Stop capture; returns the session directory."""
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

    # ---- Pause / resume / manual capture ----

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
        if not self._paused:
            return
        if self._pause_started is not None:
            self._paused_total += time.monotonic() - self._pause_started
        self._pause_started = None
        self._paused = False
        self._audio.set_paused(False)

    def manual_capture(self) -> None:
        """Hotkey screenshot at the cursor with no click behind it (result screens,
        open menus). Not gated on _mode, which only governs auto-capture."""
        if not self._running or self._paused:
            return
        from pynput.mouse import Controller

        x, y = Controller().position
        self._handle_click(int(x), int(y), "manual")

    def _now_offset_ms(self) -> int:
        """Event offset: elapsed time minus paused time, plus the resumed-session
        base. Audio also drops frames while paused, so both timelines exclude
        pauses — break either and Whisper timestamps stop matching screenshots."""
        paused = self._paused_total
        if self._paused and self._pause_started is not None:
            paused += time.monotonic() - self._pause_started
        return self._offset_base_ms + int(
            (time.monotonic() - self._start_mono - paused) * 1000
        )

    def set_name(self, name: str) -> None:
        """Set the session name and persist it immediately."""
        with self._lock:
            self._name = name.strip() or None
            if self._session_dir:
                self._flush_json()

    def add_transcript_segments(self, segments: list) -> None:
        """Install Whisper segments, REPLACING any existing ones — a resumed session
        retranscribes its whole concatenated audio."""
        with self._lock:
            self._events = [
                e for e in self._events if e.get("type") != "transcript_segment"
            ]
            self._events.extend(segments)
            self._status = "transcribed"
            self._flush_json()

    def finalize(self, stopped_at: datetime, transcript_ok: bool = True) -> None:
        """Record the stop time and write the final JSON. Call it even when
        transcription failed (transcript_ok=False), or status stays "recording"."""
        with self._lock:
            self._stopped_at = stopped_at
            self._status = "ready" if transcript_ok else "transcript_failed"
            self._flush_json()

    # ---- Click to screenshot pipeline ----

    def _handle_click(self, x: int, y: int, click_type: str) -> None:
        """Grab the screen NOW, on the caller's thread; save and inspect later.

        The grab is the one step that cannot be deferred — the click fires on
        button-down and the target window starts repainting immediately, so a
        grab handed to a worker thread races the very change it is meant to
        precede. That race is what produced blank mid-load pages. Downscaling,
        writing the PNG and the UI Automation lookup all tolerate the delay.
        """
        if not self._running or self._paused:
            return
        if self._screenshot_count >= self.config.max_screenshots_per_session:
            return

        offset_ms = self._now_offset_ms()
        try:
            img, img_x, img_y = capture_raw(x, y)
        except Exception:
            log.exception("screenshot: capture failed at (%d, %d)", x, y)
            return

        # Numbered only after a successful grab, so a failed capture doesn't
        # consume an event id or a slot against max_screenshots_per_session.
        with self._lock:
            if self._screenshot_count >= self.config.max_screenshots_per_session:
                return
            self._screenshot_count += 1
            count = self._screenshot_count

        filename = f"screenshots/{count:04d}_{offset_ms}.png"
        threading.Thread(
            target=self._save_capture,
            args=(img, img_x, img_y, x, y, click_type, offset_ms, count, filename),
            daemon=True,
        ).start()

    def _save_capture(
        self,
        img,
        img_x: int,
        img_y: int,
        x: int,
        y: int,
        click_type: str,
        offset_ms: int,
        count: int,
        filename: str,
    ) -> None:
        ui_element = None
        try:
            ui_element = get_click_target(x, y)
        except Exception:
            pass  # Best-effort; a click must never fail over this.

        try:
            # Stored raw; blur/circle effects are applied at render time.
            click_img_x, click_img_y = save_screenshot_raw(
                img, img_x, img_y, self._session_dir / filename,
                self.config.max_image_dimension,
            )
        except Exception:
            log.exception("screenshot: could not write %s", filename)
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
                # In the SAVED image's pixel space; absence marks a legacy event.
                "click_img_x": click_img_x,
                "click_img_y": click_img_y,
            },
            "edited_text": None,
        }
        if ui_element:
            event["data"]["ui_element"] = ui_element

        with self._lock:
            self._events.append(event)
            # _flush_json writes self._status, so this late thread cannot put a
            # finished session back to "recording".
            self._flush_json()

    # ---- JSON persistence ----

    def _flush_json(self) -> None:
        """Serialize current state to session.json atomically. Caller holds lock."""
        data = {
            # Keys we don't own first, so the owned keys below win on collision.
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


# ---- Post-hoc screenshot import ----

def import_screenshots(session_dir: Path, session_data: dict, image_paths: list) -> dict:
    """Append external images (any format Pillow opens) to a saved session as
    screenshot events, one second apart after the last existing offset. They carry
    no click coordinates, so event_is_legacy() sees them and they render raw.
    Mutates and returns session_data; the caller persists it.
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
