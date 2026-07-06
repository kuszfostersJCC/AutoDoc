# AutoDoc — Product Requirements Document

## Context

Educational institution staff lack documentation for their workflows. The two blockers are time and preference — many staff prefer verbally walking through a process rather than sitting down to write. AutoDoc captures that verbal walkthrough alongside click-triggered screenshots and produces a polished step-by-step markdown document, with FERPA compliance as a core constraint (audio processed locally, screenshots blurred before any cloud upload).

---

## Plan Assessment: Verdict on the Original Outline

Your outline is **sound and buildable**. A few practical refinements below:

| Your Idea | Assessment | Refinement |
|---|---|---|
| Portable app | ✅ Feasible | Distribute as a folder (`.exe` + `config.json` + `models/`), not literally a single file — Whisper's model (~150MB) must live externally |
| Transcribe speech locally | ✅ Correct call | OpenAI Whisper `base` or `small` model runs post-session on modern consumer hardware in ~20-40s |
| Screenshot on click | ✅ Straightforward | Use `pynput` for mouse hooks + `mss` for capture |
| Blur except cursor area | ✅ Feasible | Full Gaussian blur + circular composite mask around click coordinates using `Pillow` |
| String together then send to AI | ✅ Good architecture | Add a **review step** between capture and generation so users can fix/delete before sending to Claude |
| FERPA — blur before uploading | ✅ Right instinct | Combine with local-only audio transcription so raw audio never leaves the machine |

---

## Product Requirements

### P0 — Core (Must Have)

#### Recording
- System tray icon on Windows 11 (Start/Stop via right-click menu)
- Tray icon changes state: Idle → Recording → Processing → Idle
- Capture audio continuously during recording session
- Capture screenshot on **left-click** and **right-click**
- Apply blur to screenshots: full Gaussian blur, with a circular unblurred "focus window" (~200px radius) around the click coordinates. Feathered edges.
- Timestamp every event (audio position, screenshot) relative to session start

#### Transcription
- Post-session batch transcription using Whisper (`base` model default)
- Audio never leaves the machine
- Whisper segments merged with screenshot events by timestamp into a unified session document

#### Session Auto-Save
- When recording stops and transcription completes, the session is **automatically saved to disk** — no immediate review step
- Session document is a self-contained folder: `sessions/SESSION_YYYYMMDD_HHMMSS/` containing `session.json` + blurred screenshots
- User is notified via tray notification that the session was saved

#### Sessions Window
- Accessible from tray menu: "View Sessions"
- Simple list of all saved sessions (date, duration, screenshot count)
- Per-session actions: **Edit** (opens session transcript in a text editor), **Delete**, **Generate Documentation**
- "Generate Documentation" sends session to Claude API

#### Generation
- Send interleaved session events to Claude API (blurred images as base64 + transcript as text)
- Claude produces **text-only** structured markdown (title, overview, prerequisites, numbered steps with click location descriptions, notes)
- No images embedded in the final output — screenshots are used as context for Claude only
- Output: a single `workflow_name.md` file saved to the `output/` folder

#### Packaging
- Single `.exe` produced by PyInstaller (CPU-only torch to minimize size)
- Config (`config.json`) lives adjacent to `.exe` — API key stored there
- Whisper model downloaded to `models/` folder on first run (one-time, ~150MB)
- Portable: copy the AutoDoc folder to USB or network share; run directly

### P1 — Important (Should Have)

- First-run setup dialog if `config.json` is missing/invalid (prompts for API key)
- Screenshot downsampling before API upload (1280x720 max) to control cost and context window usage
- Warning dialog if a screenshot appears all-black (DRM-blocked window)
- Max screenshot count safeguard (configurable, default 100 per session)

### P2 — Nice to Have (Future)

- Mac support (pynput and mss are cross-platform; primary blocker is pystray tray behavior)
- Direct push to BookStack wiki
- Configurable focus radius and blur strength in UI
- Multiple concurrent sessions

---

## Technical Architecture

### Stack

| Concern | Library |
|---|---|
| System tray | `pystray` |
| Mouse hooks | `pynput` |
| Screen capture | `mss` |
| Image processing | `Pillow` |
| Audio recording | `sounddevice` |
| Local transcription | `openai-whisper` (base model) |
| AI documentation | `anthropic` (Claude API) |
| Review UI | `tkinter` |
| Packaging | `PyInstaller` + CPU-only `torch` |

### Module Structure

```
AutoDoc/
├── main.py                        # Entry point, state machine orchestration
├── config.py                      # Config loading (config.json adjacent to .exe)
├── config.json                    # API key + runtime settings (admin-populated)
├── recorder/
│   ├── session.py                 # Session lifecycle, event log (session.json)
│   ├── audio_capture.py           # sounddevice → WAV file
│   ├── mouse_hooks.py             # pynput global mouse listener
│   └── screenshot.py             # mss capture + Pillow blur pipeline
├── transcription/
│   └── whisper_runner.py          # Whisper load, transcribe, segment → events
├── sessions_window/
│   └── sessions_window.py         # Tkinter window: list saved sessions, edit/delete/generate
├── generation/
│   ├── claude_client.py           # Prompt construction, Anthropic API call
│   └── output_writer.py          # Write markdown to output/
├── tray/
│   └── tray_app.py               # pystray icon, menu, state transitions
├── assets/
│   ├── icon_idle.png
│   ├── icon_recording.png
│   └── icon_processing.png
└── autodoc.spec                  # PyInstaller build spec
```

### Session Data Format

Each recording creates `sessions/SESSION_YYYYMMDD_HHMMSS/` containing:
- `session.json` — unified event log (see schema below)
- `audio_full.wav` — raw PCM audio (local only, not uploaded)
- `screenshots/NNNN_<offset_ms>.png` — blurred screenshots

```json
{
  "schema": "autodoc-session/v1",
  "session_id": "20260406_143022",
  "started_at": "2026-04-06T14:30:22Z",
  "stopped_at": "2026-04-06T14:35:10Z",
  "status": "pending_review",
  "events": [
    {
      "event_id": "evt_001",
      "type": "screenshot",
      "timestamp_offset_ms": 2659,
      "data": {
        "filename": "screenshots/0001_2659.png",
        "click_type": "left",
        "click_x": 412, "click_y": 830,
        "screen_width": 1920, "screen_height": 1080
      },
      "review_state": "approved"
    },
    {
      "event_id": "evt_002",
      "type": "transcript_segment",
      "timestamp_offset_ms": 1000,
      "data": {
        "text": "First, open the student records system.",
        "whisper_start_s": 1.0, "whisper_end_s": 4.2
      },
      "review_state": "approved",
      "edited_text": null
    }
  ]
}
```

### Application State Machine

```
IDLE ──(Start)──────────────→ RECORDING
RECORDING ──(Stop)──────────→ TRANSCRIBING
TRANSCRIBING ──(done)───────→ IDLE  (tray notification: "Session saved")
IDLE ──(View Sessions)──────→ SESSIONS_WINDOW (non-blocking, separate window)
SESSIONS_WINDOW ──(Generate)→ GENERATING ──(done)──→ open output file
GENERATING ──(error)────────→ SESSIONS_WINDOW (show error, allow retry)
```

### Threading Model

- **Main thread**: pystray event loop (Win32 message pump; must be main thread)
- **Daemon thread**: pynput mouse listener
- **PortAudio callback**: sounddevice audio (internal; callback appends to deque)
- **Worker thread** (post-stop): Whisper transcription → signals main thread when done → tray notification
- **Tkinter window**: Sessions Window spawned in its own thread when user opens it
- **Worker thread** (on Generate): Claude API call

Resolution for pystray/tkinter main thread conflict: use `pystray.Icon.run_detached()` so pystray runs in its own thread, freeing the main thread for tkinter when the Sessions Window opens.

### Blur Implementation

```
1. Full screenshot via mss → PIL RGB Image
2. blurred_full = img.filter(GaussianBlur(radius=15))
3. mask = Image.new("L", img.size, 0)           # black = show blurred
4. Draw filled white ellipse on mask at (click_x, click_y), radius=200px
5. mask = mask.filter(GaussianBlur(radius=20))  # feather edges
6. result = Image.composite(img, blurred_full, mask)
   # where mask=255 → original; where mask=0 → blurred
```

### Claude Prompt Structure

System prompt instructs Claude to output: `# Title`, `## Overview`, `## Prerequisites`, `## Steps` (numbered, each with screenshot reference), `## Notes`.

User message content blocks are interleaved chronologically:
```
[NARRATION at 1000ms]: First, open the student records system.
[IMAGE: 0001_2659.png]
[IMAGE: filename tag follows each image block]
[NARRATION at 5200ms]: Click the Search tab at the top.
[IMAGE: 0002_5440.png]
...
```

### Key FERPA Boundaries

| Data | Where Processed | Uploaded? |
|---|---|---|
| Raw audio | Local only (Whisper) | Never |
| Audio transcript (text) | Local → Claude API | Yes — text only |
| Screenshots (blurred) | Local blur → Claude API | Yes — blurred PNGs |
| Session JSON | Local disk | Never (unless user copies) |

**Residual risk**: transcript may contain student names spoken aloud. This is acknowledged and mitigated by the blur (visual PII) but not eliminated for audio PII. Recommend instructing users to avoid naming students during recordings.

---

## Packaging Approach

- Build with PyInstaller `--onefile` (single `.exe`)
- Use **CPU-only torch** (`pip install torch --index-url https://download.pytorch.org/whl/cpu`) to prevent ~2GB CUDA bloat
- Expected `.exe` size: ~400-600MB (with `base` Whisper model bundled) or ~100-150MB (model downloaded separately)
- **Recommended distribution**: ship as a folder — `AutoDoc.exe` + `config.json` (admin pre-fills API key) + `models/` (populated on first run)
- Required `hiddenimports`: `pynput.mouse._win32`, `pynput.keyboard._win32`, `tiktoken_ext.openai_public`, `sounddevice`, `whisper`

### Critical Gotcha: AV/SmartScreen

Global mouse hooks will trigger antivirus false positives. Mitigate by:
1. Code-signing the `.exe` (institutional certificate preferred)
2. IT whitelist if code signing is not possible
3. Without either, Windows SmartScreen will block first launch — users must click "More info → Run anyway"

---

## Build Sequence (Implementation Order)

1. `config.py` + `config.json`
2. `recorder/audio_capture.py` (test standalone)
3. `recorder/screenshot.py` (test standalone — verify blur output)
4. `recorder/mouse_hooks.py` (test with print callback)
5. `recorder/session.py` (integrates 2-4, auto-saves on stop)
6. `tray/tray_app.py` (wire session lifecycle to tray menu, tray notification on save)
7. `transcription/whisper_runner.py` (test with a WAV file)
8. `sessions_window/sessions_window.py` (develop with mock session list)
9. `generation/claude_client.py` (test with saved session)
10. `generation/output_writer.py`
11. `main.py` (final integration)
12. `autodoc.spec` + packaging verification

---

## Verification

- **Screenshot blur**: Run `screenshot.py` standalone, click somewhere, inspect output PNG — non-click area must be unrecognizable
- **Whisper transcription**: Feed a known WAV file, verify segment timestamps match expected offsets
- **Sessions Window**: Load a directory of saved sessions, verify list displays, edit/delete/generate actions work
- **Claude generation**: Use a saved session folder to test `claude_client.py` without live recording; verify output is text-only markdown with no image references
- **Full end-to-end**: Record a 2-minute demo workflow, verify auto-save, open Sessions Window, generate documentation, inspect markdown output
- **Packaging**: Copy the output folder to a different machine (no Python installed), verify `.exe` launches and completes a full session
- **AV test**: Run Windows Defender scan on the packaged `.exe` — note any flags for IT communication

---

## Open Questions for Later

1. **Cost control**: Who monitors Anthropic API usage? Consider adding a per-session token/cost estimate before generation.
2. **Session storage**: Sessions accumulate on disk. Add a cleanup policy or manual "clear old sessions" option.
3. **BookStack integration**: Confirm BookStack API availability and auth method when this becomes a P1 priority.
4. **Code signing**: Does your institution have a code signing cert, or will you purchase one ($100-400/yr)?
