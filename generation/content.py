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

SYSTEM_PROMPT = """\
You are a technical documentation assistant. Your task is to write clear, \
step-by-step workflow documentation for non-technical staff at an educational institution.

You will receive a narration of a workflow and, in most cases, a sequence of screenshots. \
Each screenshot has a privacy blur applied — the blurred areas conceal surrounding \
content, while a circular clear region highlights exactly where the user clicked. \
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
relevant control — reference what is visible in the unblurred area of the screenshot \
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
- Describe click locations in plain terms ("the Search tab at the top of the page"), \
not pixel coordinates.
- Do NOT invent steps that are not evidenced by the narration or screenshots.
- Do NOT include any markdown image syntax or image references in your output.
- Output ONLY the markdown document — no preamble, no closing remarks.\
"""


def _screenshot_blocks(events: list[dict], session_dir: Path) -> list[dict]:
    """Return neutral image blocks for all surviving screenshot events."""
    blocks: list[dict] = []
    screenshots = sorted(
        [e for e in events if e.get("type") == "screenshot"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    for event in screenshots:
        img_path = session_dir / event["data"]["filename"]
        if not img_path.exists():
            continue
        offset_s = event["timestamp_offset_ms"] / 1000
        click_type = event["data"].get("click_type", "left")
        cx = event["data"].get("click_x", "?")
        cy = event["data"].get("click_y", "?")

        blocks.append({
            "type": "text",
            "text": (
                f"[SCREENSHOT at {offset_s:.1f}s — "
                f"{click_type}-click at screen position ({cx}, {cy})]:"
            ),
        })
        blocks.append({"type": "image", "path": img_path})
    return blocks


def build_content_blocks(session_data: dict, session_dir: Path) -> list[dict]:
    """
    Build the full list of neutral content blocks for a session.

    If edited_full_transcript is present, it is placed first as a complete
    narration block, followed by screenshots in chronological order.

    Otherwise, Whisper segments and screenshots are interleaved by timestamp.
    Sessions with no screenshots at all (transcript-only recordings) naturally
    fall out of this — the returned list simply contains no image blocks.
    """
    events = session_data.get("events", [])
    edited_transcript = session_data.get("edited_full_transcript", "").strip()

    if edited_transcript:
        blocks: list[dict] = [
            {
                "type": "text",
                "text": f"[FULL NARRATION (user-reviewed)]:\n{edited_transcript}",
            }
        ]
        blocks += _screenshot_blocks(events, session_dir)
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
            img_path = session_dir / event["data"]["filename"]
            if not img_path.exists():
                continue
            click_type = event["data"].get("click_type", "left")
            cx = event["data"].get("click_x", "?")
            cy = event["data"].get("click_y", "?")
            blocks.append({
                "type": "text",
                "text": (
                    f"[SCREENSHOT at {offset_s:.1f}s — "
                    f"{click_type}-click at screen position ({cx}, {cy})]:"
                ),
            })
            blocks.append({"type": "image", "path": img_path})

    return blocks
