"""
generation/exporter.py — Export a session as a self-contained Claude-ready folder.

Creates:
    exports/{session_name}_{session_id}/
        INSTRUCTIONS.md     — Complete prompt + narration Claude needs to generate docs
        session_meta.json   — Metadata and a "processed" flag for the scheduled agent
        images/
            0001_2659.png   — Copies of the blurred screenshots
            ...

The same folder works for two workflows:
  • Manual (Claude Desktop) — user uploads INSTRUCTIONS.md + all images to a conversation
  • Automated (Cowork agent) — scheduled task scans exports/, finds processed=false, handles each

zip_export_folder() packs that folder into a single .zip and (by default)
DELETES the folder afterwards — the zip is the deliverable, and keeping both
doubled the disk footprint for no benefit. This is for the "coworker records,
admin generates" handoff: a non-technical coworker with no API key/local model
configured can hit Export, then just attach one .zip file to an email or Teams
message instead of gathering a folder of loose images.
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from recorder.screenshot import render_event_to_cache


# ------------------------------------------------------------------ #
# Prompt template written into INSTRUCTIONS.md                        #
# ------------------------------------------------------------------ #

_INSTRUCTIONS_TEMPLATE = """\
# AutoDoc Export: {session_name}

## How to Use This File

**Claude Desktop (manual):**
1. Open a new conversation in Claude Desktop
2. Upload this file (`INSTRUCTIONS.md`) and every image in the `images/` folder
3. Send — Claude will generate the documentation

**Automated (Cowork scheduled agent):**
This folder will be picked up and processed automatically.
Do not modify `session_meta.json` by hand.

---

## Your Task

You are a technical documentation assistant. Generate clear, step-by-step workflow
documentation for non-technical staff at an educational institution.

Produce a markdown document in this exact structure:

```
# [Concise workflow title derived from the narration — max 8 words]

## Overview
[2–3 sentences describing what this workflow does and when staff would use it.]

## Prerequisites
[Bullet list of anything needed beforehand. Write "None" if nothing is mentioned.]

## Steps

### Step 1: [Short imperative title]
[Plain-language explanation. One action per step. Describe on-screen locations in plain
terms ("the blue Save button in the upper-right corner"). Reference visible details in
the screenshots.]

### Step 2: ...

## Notes
[Warnings, tips, or edge cases from the narration. Omit section if none.]
```

Rules:
- Write for a non-technical audience (6th-grade reading level).
- Each step = ONE distinct action or click.
- Use narration as primary truth; screenshots confirm visual context.
- Do NOT invent steps not evidenced by narration or screenshots.
- Do NOT include image references or markdown image syntax in output.
- Output ONLY the markdown document.

---

## Session Information

- **Name:** {session_name}
- **Session ID:** {session_id}
- **Recorded:** {recorded_at}
- **Duration:** {duration}

---

## Narration

{narration}

---

## Screenshots

The following images are included in the `images/` subfolder. {screenshot_description}

{screenshot_table}
"""

# Template for transcript-only sessions — no image upload instructions or screenshot table.
_INSTRUCTIONS_TRANSCRIPT_ONLY_TEMPLATE = """\
# AutoDoc Export (Transcript Only): {session_name}

## How to Use This File

**Claude Desktop (manual):**
1. Open a new conversation in Claude Desktop
2. Upload this file (`INSTRUCTIONS.md`)
3. Send — Claude will generate the documentation from the narration alone

**Automated (Cowork scheduled agent):**
This folder will be picked up and processed automatically.

> **Note:** This is a transcript-only session. No screenshots were captured.
> Documentation is generated purely from the spoken narration.

---

## Your Task

You are a technical documentation assistant. Generate clear, step-by-step workflow
documentation for non-technical staff at an educational institution.
You have only the spoken narration to work from — there are no screenshots.
Use the narration as your complete source of truth.

Produce a markdown document in this exact structure:

```
# [Concise workflow title — max 8 words]

## Overview
[2–3 sentences describing what this workflow does and when staff would use it.]

## Prerequisites
[Bullet list of anything needed beforehand. Write "None" if nothing is mentioned.]

## Steps

### Step 1: [Short imperative title]
[Plain-language explanation. Describe each action clearly. Since there are no screenshots,
be specific about where things are located on screen based on the narration.]

### Step 2: ...

## Notes
[Warnings, tips, or edge cases from the narration. Omit section if none.]
```

Rules:
- Write for a non-technical audience (6th-grade reading level).
- Each step = ONE distinct action.
- Do NOT invent steps not evidenced by the narration.
- Output ONLY the markdown document.

---

## Session Information

- **Name:** {session_name}
- **Session ID:** {session_id}
- **Recorded:** {recorded_at}
- **Duration:** {duration}

---

## Narration

{narration}
"""


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #

def export_session(
    session_data: dict,
    session_dir: Path,
    exports_root: Path,
    config=None,
) -> Path:
    """
    Build a self-contained export folder from a session.

    Screenshots are RENDERED (raw/blurred/circled, per the session's saved
    render mode) into the export — the raw on-disk PNGs never leave the
    session folder unless the mode is explicitly "raw". This matters: the
    export is the artifact that gets emailed/uploaded, so it must respect
    the privacy mode even though local storage is raw.

    Args:
        session_data:   Parsed session.json dict.
        session_dir:    Source session folder (where screenshots live).
        exports_root:   Root exports/ directory (created if absent).
        config:         AutoDocConfig; supplies the default render mode and
                        blur/circle parameters. None falls back to legacy
                        copy-as-is behaviour.

    Returns:
        Path to the created export folder.
    """
    exports_root.mkdir(parents=True, exist_ok=True)

    if config is not None:
        from generation.content import SCREENSHOT_DESCRIPTIONS, resolve_session_mode
        render_mode = resolve_session_mode(session_data, config)
        screenshot_description = SCREENSHOT_DESCRIPTIONS.get(
            render_mode, SCREENSHOT_DESCRIPTIONS["blurred"]
        )
    else:
        render_mode = None  # copy files untouched
        screenshot_description = (
            "Each screenshot has a privacy blur applied — the background is blurred "
            "and a circular clear window highlights exactly where the user clicked."
        )

    session_id = session_data.get("session_id", "session")
    name = session_data.get("name") or session_id
    folder_stem = _safe_stem(name, session_id)
    export_dir = _unique_path(exports_root / folder_stem)
    export_dir.mkdir(parents=True)

    images_dir = export_dir / "images"
    images_dir.mkdir()

    # ── Copy screenshots ──────────────────────────────────────── #
    events = session_data.get("events", [])
    screenshots = sorted(
        [e for e in events if e.get("type") == "screenshot"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    copied_images: list[dict] = []
    for evt in screenshots:
        if render_mode is not None:
            src = render_event_to_cache(session_dir, evt, render_mode, config)
        else:
            src = session_dir / evt["data"]["filename"]
        if src is None or not src.exists():
            continue
        dest = images_dir / src.name
        shutil.copy2(src, dest)
        copied_images.append(
            {
                "filename": src.name,
                "offset_s": evt["timestamp_offset_ms"] / 1000,
                "click_type": evt["data"].get("click_type", "left"),
            }
        )

    # ── Build narration text ──────────────────────────────────── #
    narration = _extract_narration(session_data)

    # ── Build screenshot table ────────────────────────────────── #
    if copied_images:
        rows = ["| File | Timestamp | Click |", "|------|-----------|-------|"]
        for img in copied_images:
            rows.append(
                f"| `{img['filename']}` | {img['offset_s']:.1f}s | {img['click_type']}-click |"
            )
        screenshot_table = "\n".join(rows)
    else:
        screenshot_table = "_No screenshots in this session._"

    # ── Format dates ──────────────────────────────────────────── #
    started_raw = session_data.get("started_at", "")
    stopped_raw = session_data.get("stopped_at", "")
    recorded_at = _fmt_datetime(started_raw)
    duration = _fmt_duration(started_raw, stopped_raw)

    # ── Write INSTRUCTIONS.md ─────────────────────────────────── #
    # Branch on whether the session ACTUALLY has screenshots (copied_images),
    # not on session_type. session_type only ever meant "were clicks
    # auto-captured" — a "transcript_only" session can have screenshots now
    # via recorder/session.py::import_screenshots(), and that must still get
    # the full image-aware template. See session_editor.py's module
    # docstring for the same reasoning applied to the editor UI.
    if not copied_images:
        instructions = _INSTRUCTIONS_TRANSCRIPT_ONLY_TEMPLATE.format(
            session_name=name,
            session_id=session_id,
            recorded_at=recorded_at,
            duration=duration,
            narration=narration or "_No narration was recorded for this session._",
        )
    else:
        instructions = _INSTRUCTIONS_TEMPLATE.format(
            session_name=name,
            session_id=session_id,
            recorded_at=recorded_at,
            duration=duration,
            narration=narration or "_No narration was recorded for this session._",
            screenshot_table=screenshot_table,
            screenshot_description=screenshot_description,
        )
    (export_dir / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")

    # ── Write session_meta.json ───────────────────────────────── #
    meta = {
        "session_id": session_id,
        "name": name,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "processed": False,
        "output_file": None,
    }
    (export_dir / "session_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    return export_dir


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _extract_narration(session_data: dict) -> str:
    """Return the best available narration text from a session."""
    edited = session_data.get("edited_full_transcript", "").strip()
    if edited:
        return edited

    segments = sorted(
        [e for e in session_data.get("events", []) if e.get("type") == "transcript_segment"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    return "\n\n".join(
        (e.get("edited_text") or e["data"]["text"]).strip()
        for e in segments
        if (e.get("edited_text") or e["data"].get("text", "")).strip()
    )


def _safe_stem(name: str, fallback: str) -> str:
    safe = re.sub(r"[^\w\s-]", "", name)
    safe = re.sub(r"\s+", "_", safe.strip())[:60]
    return f"{safe}_{fallback}" if safe else fallback


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.parent / f"{path.name}_{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def _fmt_datetime(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%B %d, %Y at %I:%M %p UTC")
    except (ValueError, TypeError):
        return iso or "Unknown"


def _fmt_duration(started: str, stopped: str) -> str:
    try:
        s = datetime.fromisoformat(started)
        e = datetime.fromisoformat(stopped)
        total = int((e - s).total_seconds())
        return f"{total // 60}m {total % 60:02d}s"
    except (ValueError, TypeError):
        return "Unknown"


# ------------------------------------------------------------------ #
# Zipping (for the "coworker exports, admin generates" handoff)       #
# ------------------------------------------------------------------ #

def zip_export_folder(export_dir: Path, delete_folder: bool = True) -> Path:
    """
    Pack an export folder (as produced by export_session) into a single
    .zip sitting alongside it, e.g. exports/My_Workflow_20260406_173051.zip,
    then delete the folder (the zip is the only deliverable).

    Returns the path to the created zip file.
    """
    zip_base = export_dir.parent / export_dir.name
    zip_path = shutil.make_archive(
        base_name=str(zip_base),
        format="zip",
        root_dir=str(export_dir.parent),
        base_dir=export_dir.name,
    )
    if delete_folder:
        shutil.rmtree(export_dir, ignore_errors=True)
    return Path(zip_path)
