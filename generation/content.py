"""
generation/content.py — Provider-agnostic prompt and content-block construction.

This module builds the system prompt and a *neutral* list of content blocks from a
session (narration + screenshots). It knows nothing about Anthropic, OpenAI, or any
other wire format — that translation happens in generation/backends/. Keeping this
layer neutral is what makes it possible to swap generation providers (cloud API vs.
a local vision model) without touching prompt logic, session parsing, or the UI.

Neutral content block shapes:
    {"type": "text", "text": str}
    {"type": "image", "path": Path}   # PNG already saved to disk; backends read bytes
"""

from pathlib import Path

from recorder.screenshot import (
    event_is_legacy,
    render_event_to_cache,
    resolve_render_mode,
)

# Screenshot-handling sentence, per render mode. Injected into the system
# prompt so the model's expectations match what the images actually look like.
SCREENSHOT_DESCRIPTIONS = {
    "raw": (
        "Screenshots are unmodified screen captures. The click position for each "
        "screenshot is given in its accompanying text label."
    ),
    "blurred": (
        "Each screenshot has a privacy blur applied — the blurred areas conceal "
        "surrounding content, while a circular clear region highlights exactly "
        "where the user clicked."
    ),
    "circled": (
        "Each screenshot is an unmodified capture with a light red circle drawn "
        "around the exact UI element the user clicked. Treat the circled element "
        "as the target of that step's action."
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """\
You are a technical documentation assistant. Your task is to write clear, \
step-by-step workflow documentation for non-technical staff at an educational institution.

You will receive a narration of a workflow and, in most cases, a sequence of screenshots. \
{screenshot_description} \
If no screenshots are provided, use the narration alone as your complete source of truth.

Produce a markdown document in this exact structure:

# [Derive a concise workflow title from the narration — max 8 words]

## Overview
[2–3 sentences describing what the workflow accomplishes and when staff would use it.]

## Prerequisites
[Bullet list of anything needed beforehand: logins, permissions, files, etc. \
Write "None" if nothing is mentioned.]

## Steps

### Step 1: [Short imperative title, e.g. "Open the Student Information System"]
[Plain-language explanation. Describe the action and where on screen to find the \
relevant control — reference what is visible in the screenshot \
(e.g., "the blue Submit button in the upper-right corner"). One action per step.]

### Step 2: [Short imperative title]
[Continue for each distinct action or decision point observed in the recording.]

## Notes
[Any warnings, tips, exceptions, or edge cases mentioned in the narration. \
Omit this entire section if none were mentioned.]

Rules:
- Write for a non-technical audience (target a 6th-grade reading level).
- Number steps sequentially starting at 1.
- Each step corresponds to ONE distinct action or click.
- Use the narration as the primary source of truth; screenshots confirm visual context.
- Some screenshots include a "clicked element (ground truth)" label captured directly \
from the operating system at click time. Treat these element names and window titles as \
authoritative: use them verbatim when naming buttons, fields, menus, and windows in steps. \
Never contradict a ground-truth label based on what you think you see in the image.
- Describe click locations in plain terms ("the Search tab at the top of the page"), \
not pixel coordinates.
- Do NOT invent steps that are not evidenced by the narration or screenshots.
- PLACING SCREENSHOTS: each screenshot is labeled with an id like evt_0004. To include \
a screenshot in the document, put the token {{{{screenshot:evt_0004}}}} COMPLETELY ALONE \
on its own line, immediately after the step text it illustrates. Never put a token inside \
a sentence, inside parentheses, or on the same line as any other text — wrong: \
"Click Save. (See {{{{screenshot:evt_0004}}}})", right: the sentence on one line, then the \
token by itself on the next line. Use each screenshot at most once, keep tokens in \
chronological order, and include a screenshot for every step where one exists.
- Do NOT use markdown image syntax (![](...)) — screenshot tokens are the only way to \
reference images.
- Output ONLY the markdown document — no preamble, no closing remarks.\
"""


def resolve_session_mode(session_data: dict, config) -> str:
    """
    The render mode this session's images will actually use. Legacy sessions
    (pre-raw-storage, destructively blurred PNGs) are always "blurred"
    regardless of settings, because that's what the pixels are.
    """
    events = session_data.get("events", [])
    screenshots = [e for e in events if e.get("type") == "screenshot"]
    if screenshots and all(event_is_legacy(e) for e in screenshots):
        return "blurred"
    return resolve_render_mode(session_data, config)


def get_system_prompt(mode: str) -> str:
    """System prompt with the screenshot description matching the render mode."""
    desc = SCREENSHOT_DESCRIPTIONS.get(mode, SCREENSHOT_DESCRIPTIONS["blurred"])
    return _SYSTEM_PROMPT_TEMPLATE.format(screenshot_description=desc)


def _describe_click(event: dict) -> str:
    """
    Human-readable description of the interaction behind a screenshot event.

    When the recorder captured the clicked element (data.ui_element, PRD F1),
    that is included as GROUND TRUTH — deterministic at capture time via
    Win32/UI Automation, unlike anything inferred from pixels. The system
    prompt instructs the model to prefer these names over visual guessing.
    """
    data = event.get("data", {})
    click_type = data.get("click_type", "left")

    if click_type == "imported":
        # Appended post-hoc via recorder/session.py::import_screenshots() —
        # no click ever happened, so don't tell the model one did (e.g. a
        # phone photo of a BIOS screen manually added to a session).
        return "manually imported photo (no click coordinates — added to this session after recording)"

    cx = data.get("click_x", "?")
    cy = data.get("click_y", "?")

    if click_type == "manual":
        desc = f"manual capture at screen position ({cx}, {cy})"
    else:
        desc = f"{click_type}-click at screen position ({cx}, {cy})"

    ui = data.get("ui_element") or {}
    if ui.get("element_name"):
        role = f" {ui['element_role']}" if ui.get("element_role") else ""
        desc += f" — clicked element (ground truth): the{role} \"{ui['element_name']}\""
    if ui.get("window_title"):
        desc += f" — window: \"{ui['window_title']}\""
    if ui.get("app_name"):
        desc += f" ({ui['app_name']})"
    return desc


def _screenshot_event_blocks(event: dict, session_dir: Path, mode: str, config) -> list[dict]:
    """Neutral text+image blocks for one screenshot event, rendered per mode."""
    img_path = render_event_to_cache(session_dir, event, mode, config)
    if img_path is None:
        return []
    offset_s = event["timestamp_offset_ms"] / 1000
    return [
        {
            "type": "text",
            "text": (
                f"[SCREENSHOT id={event['event_id']} at {offset_s:.1f}s — "
                f"{_describe_click(event)}]:"
            ),
        },
        {"type": "image", "path": img_path},
    ]


def build_content_blocks(session_data: dict, session_dir: Path, config) -> list[dict]:
    """
    Build the full list of neutral content blocks for a session. Screenshot
    images are rendered (blur/circle/raw per the session's mode) into
    session_dir/rendered/ and the rendered paths are what backends read.

    If edited_full_transcript is present, it is placed first as a complete
    narration block, followed by screenshots in chronological order.

    Otherwise, Whisper segments and screenshots are interleaved by timestamp.
    Sessions with no screenshots at all (transcript-only recordings) naturally
    fall out of this — the returned list simply contains no image blocks.
    """
    events = session_data.get("events", [])
    edited_transcript = session_data.get("edited_full_transcript", "").strip()
    mode = resolve_session_mode(session_data, config)

    if edited_transcript:
        blocks: list[dict] = [
            {
                "type": "text",
                "text": f"[FULL NARRATION (user-reviewed)]:\n{edited_transcript}",
            }
        ]
        screenshots = sorted(
            [e for e in events if e.get("type") == "screenshot"],
            key=lambda e: e.get("timestamp_offset_ms", 0),
        )
        for event in screenshots:
            blocks += _screenshot_event_blocks(event, session_dir, mode, config)
        return blocks

    # No edited transcript — interleave Whisper segments with screenshots.
    ordered = sorted(events, key=lambda e: e.get("timestamp_offset_ms", 0))
    blocks = []
    for event in ordered:
        etype = event.get("type")
        offset_s = event.get("timestamp_offset_ms", 0) / 1000

        if etype == "transcript_segment":
            text = (event.get("edited_text") or event["data"]["text"]).strip()
            if text:
                blocks.append({
                    "type": "text",
                    "text": f"[NARRATION at {offset_s:.1f}s]: {text}",
                })

        elif etype == "screenshot":
            blocks += _screenshot_event_blocks(event, session_dir, mode, config)

    return blocks
