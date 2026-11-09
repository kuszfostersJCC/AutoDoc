"""Builds the system prompt and the neutral text/image content blocks for a session.

The markdown subset the prompt permits must stay in step with what
generation/renderers.py::_iter_blocks() can parse.
"""

import re
from pathlib import Path

from recorder.screenshot import (
    event_is_legacy,
    event_note,
    render_event_to_cache,
    resolve_render_mode,
)
from recorder.transcript import ordered_segments, segment_text

# Injected into the system prompt so the model's expectations match the images.
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

You will receive, in this order: a SESSION CONTEXT block of background facts recorded \
by the tool, sometimes a reviewed FULL NARRATION, then the recording's timeline — what \
was said, interleaved with the screenshots taken at those moments. \
{screenshot_description} \
If no screenshots are provided, use the narration alone as your complete source of truth.

The SESSION CONTEXT block is background, never a step: use it to name the systems \
involved and to write the Overview and Prerequisites, and never quote it as if it were \
something the person said. If a FULL NARRATION block is present, its wording is \
authoritative and the timeline that follows exists only to tell you which screenshot \
belongs where — the two describe ONE procedure, so never document the same procedure \
twice.

Produce a markdown document in this exact structure:

# [Derive a concise workflow title from the narration — max 8 words. Name the task \
itself, e.g. "Log In to Canvas". No colon, no subtitle, and never append "A \
Step-by-Step Guide" or similar — the reader can see what kind of document this is.]

## Overview
[2–3 sentences describing what the workflow accomplishes and when staff would use it.]

## Prerequisites
[Bullet list of anything needed beforehand: logins, permissions, files, etc. \
Indent to nest a sub-point under its parent. Write "None" if nothing is mentioned.]

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
- Every step must make sense to someone who CANNOT see the screenshot. Say what the \
reader does and what it achieves — "Open the Logins menu and choose Canvas Courses" — \
never merely restate what was clicked. If you cannot explain what a click was for, say \
what it achieved rather than describing the pixels.
- Use the narration as the primary source of truth; screenshots confirm visual context.
- Photos listed as added after the recording have no reliable position in the timeline. \
Work out where each belongs from the narration and place it at that step, rather than \
assuming it comes last.
- Each screenshot's label says whether the operating system could identify the control \
that was clicked. When it says a name is RELIABLE, use that name exactly as written, \
including its capitalisation — it came from the application itself and beats anything you \
think you can read in the image. When it says NO named control could be identified, the \
label has nothing worth quoting: look at the screenshot, work out which control was \
clicked, and describe it in your own plain words.
- Never write a step around words that are obviously not a control's label, even if they \
appear in a label. Page titles, position indicators like "1 of 2", and sentences of \
on-screen prose are not things a reader can click. "Click the pane titled 1 of 2" and \
"click the document titled Sign in to your account" are both wrong — say what the reader \
would actually do: "click Canvas Courses in the Logins menu", "sign in with your college \
account".
- Do NOT put window or title-bar text in step text. "the Home Page - Jackson College - \
Google Chrome window" is title-bar text, not something a reader recognises. Window names \
in a label describe the screen BEFORE the click, so on any step that opens a new page they \
name the page being left — which makes them actively misleading. Name a system in the \
Overview and Prerequisites, and only mention switching windows in a step when the step \
really is "switch to another application".
- Do NOT reproduce personal or identifying information visible in the screenshots or \
labels: nobody's name, student ID, email address, phone number, or account number belongs \
in a procedure other people will follow. Write "your name", "the student's ID", "your \
email address" instead. A greeting like "Hello, Jane Smith!" is a placeholder for whoever \
is signed in — refer to it as the welcome message, not by the name it happens to show.
- Describe click locations in plain terms ("the Search tab at the top of the page"), \
not pixel coordinates.
- Do NOT invent steps that are not evidenced by the narration or screenshots.
- FORMATTING YOU MAY USE, wherever it genuinely makes something easier to follow: \
bulleted lists, with indentation to nest a sub-point under its parent; numbered \
lists, for things that must happen in a fixed order INSIDE one step; tables with a \
header row, written with pipes; **bold**; *italic*; and `inline code` for text the \
reader has to type or a value they have to match exactly. A table earns its place \
when the information is genuinely two-dimensional — field names beside the values \
to enter, or options beside what each one does. Never use a table for a sequence of \
actions; that is what the steps are for. Prefer a plain sentence when a plain \
sentence is clearer, and never add structure just to look thorough.
- FORMATTING THAT WILL BREAK: do not use fenced code blocks (```), block quotes \
(>), horizontal rules (---), underscores for emphasis (_like this_), headings \
deeper than ####, or any HTML. These are not converted, so they reach the reader \
as raw punctuation in the finished Word document.
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
    """The render mode this session's images will use.

    Legacy sessions are always "blurred" because their stored PNGs already are.
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


# UIA control types whose name is presented to the model as a reusable label.
_ACTIONABLE_ROLES = {
    "button", "checkbox", "combo box", "link", "list item", "menu item",
    "radio button", "split button", "tab", "tab item", "text field",
    "tree item", "slider", "spinner", "menu", "toolbar", "header item",
    "data item",
}

# Carousel/position indicators ("1 of 2") are never a control label.
_POSITION_NAME_RE = re.compile(r"^\d+\s*(of|/)\s*\d+$", re.IGNORECASE)

# Past this length the "name" is page prose, not a label.
_MAX_LABEL_CHARS = 60

# Title-bar decoration; the model quotes it verbatim into step text if it sees it.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*("
    r"Google Chrome|Chromium|Mozilla Firefox|Microsoft. Edge|Microsoft Edge"
    r"|Brave|Opera|Safari|Internet Explorer"
    r")\s*$",
    re.IGNORECASE,
)
# Edge inserts a profile segment ("… - Work - Microsoft Edge"), so a second,
# bounded pass is needed; a loop would eat legitimate trailing segments.
_TITLE_PROFILE_RE = re.compile(r"\s*[-–—]\s*(Work|Personal|Profile \d+)\s*$", re.IGNORECASE)


def clean_window_title(title: str) -> str:
    """Strip browser and profile decoration from a window title; never raises."""
    cleaned = (title or "").strip()
    cleaned = _TITLE_SUFFIX_RE.sub("", cleaned)
    cleaned = _TITLE_PROFILE_RE.sub("", cleaned)
    return cleaned.strip()


def _label_is_unusable(name: str) -> bool:
    """True when a UIA name is a position indicator or page prose, not a control label.

    Name-equals-page-title is deliberately left to _describe_click's role check,
    which catches it without rejecting legitimate labels.
    """
    name = (name or "").strip()
    if not name:
        return True
    if _POSITION_NAME_RE.match(name):
        return True
    return "\n" in name or len(name) > _MAX_LABEL_CHARS


def _describe_click(event: dict) -> str:
    """Human-readable description of the interaction behind a screenshot event.

    Tells the model whether the captured control name is trustworthy, rather than
    presenting every capture as ground truth.
    """
    data = event.get("data", {})
    click_type = data.get("click_type", "left")

    if click_type == "imported":
        return "manually imported photo (no click coordinates — added to this session after recording)"

    cx = data.get("click_x", "?")
    cy = data.get("click_y", "?")

    if click_type == "manual":
        desc = f"manual capture at screen position ({cx}, {cy})"
    else:
        desc = f"{click_type}-click at screen position ({cx}, {cy})"

    ui = data.get("ui_element") or {}
    name = (ui.get("element_name") or "").strip()
    role = (ui.get("element_role") or "").strip().casefold()
    window_title = ui.get("window_title") or ""

    trusted = name and role in _ACTIONABLE_ROLES and not _label_is_unusable(name)

    if trusted:
        desc += (
            f" — the operating system reports the clicked control as: "
            f"the {role} named \"{name}\". This name is reliable; use it exactly."
        )
    else:
        container = role or "an unnamed element"
        detail = f"it reported {container}" if role else "it reported nothing usable"
        desc += (
            f" — the operating system could NOT identify a named control here "
            f"({detail}), which happens when a click lands on a container or on "
            f"plain text rather than a button. Work out what was clicked from the "
            f"screenshot and describe it in your own words."
        )
        if name and not _POSITION_NAME_RE.match(name):
            # Framed as context, never as a name: the model quotes anything called a
            # name, and this text often contains whoever was signed in.
            snippet = name if len(name) <= _MAX_LABEL_CHARS else name[:_MAX_LABEL_CHARS] + "…"
            desc += (
                f" (text found at that spot, to help you understand what was "
                f"clicked — NOT a control name and NOT to be quoted: \"{snippet}\")"
            )

    cleaned_title = clean_window_title(window_title)
    if cleaned_title:
        # Captured at click time, so on a navigating click it names the page being left.
        desc += f" — window before this click: \"{cleaned_title}\""
    if ui.get("app_name"):
        desc += f" ({ui['app_name']})"
    return desc


def _screenshot_event_blocks(event: dict, session_dir: Path, mode: str, config) -> list[dict]:
    """Neutral text+image blocks for one screenshot event, rendered per mode."""
    img_path = render_event_to_cache(session_dir, event, mode, config)
    if img_path is None:
        return []
    offset_s = event["timestamp_offset_ms"] / 1000
    blocks = [
        {
            "type": "text",
            "text": (
                f"[SCREENSHOT id={event['event_id']} at {offset_s:.1f}s — "
                f"{_describe_click(event)}]:"
            ),
        }
    ]
    note = event_note(event)
    if note:
        # Before the image: a note's job is to say what to look for in it.
        blocks.append({
            "type": "text",
            "text": (
                "[NOTE FROM THE PERSON WHO RECORDED THIS, about the screenshot "
                f"below]: {note}"
            ),
        })
    blocks.append({"type": "image", "path": img_path})
    return blocks


def _is_imported(event: dict) -> bool:
    """A photo added after recording — its timestamp is ordering, not timing."""
    return event.get("data", {}).get("click_type") == "imported"


def _format_duration(session_data: dict) -> str:
    """Recording length as "4m 12s", from the last event's offset.

    Not started_at/stopped_at: those are wall-clock and include idle pauses.
    """
    events = session_data.get("events", [])
    last_ms = max((e.get("timestamp_offset_ms", 0) for e in events), default=0)
    total_s = int(last_ms / 1000)
    return f"{total_s // 60}m {total_s % 60:02d}s"


# Caps the preamble's app list on screenshot-heavy sessions.
_MAX_OBSERVED_APPS = 12


def _observed_applications(screenshots: list[dict]) -> tuple[list[str], int]:
    """Distinct "app — window title" pairs from the UIA capture, in first-seen order.

    Returns (labels, number_dropped_over_the_cap).
    """
    seen: list[str] = []
    dropped = 0
    for event in screenshots:
        ui = event.get("data", {}).get("ui_element") or {}
        app = (ui.get("app_name") or "").strip()
        window = clean_window_title(ui.get("window_title") or "")
        if not app and not window:
            continue
        label = f"{app} — \"{window}\"" if app and window else (app or f"\"{window}\"")
        if label in seen:
            continue
        if len(seen) >= _MAX_OBSERVED_APPS:
            dropped += 1
            continue
        seen.append(label)
    return seen, dropped


def _session_preamble(session_data: dict, screenshots: list[dict]) -> str:
    """Context the recording already knows, as one leading text block."""
    lines = ["[SESSION CONTEXT — background, not narration. Do not turn these into steps.]"]

    name = (session_data.get("name") or "").strip()
    if name:
        lines.append(f"- The person recording named this session: \"{name}\"")

    lines.append(f"- Recording length: {_format_duration(session_data)}")

    live = [e for e in screenshots if not _is_imported(e)]
    imported = [e for e in screenshots if _is_imported(e)]
    if live:
        lines.append(f"- Screenshots captured during the recording: {len(live)}")
    if imported:
        lines.append(
            f"- Photos added afterwards (no click, no reliable timing): {len(imported)}"
        )
    if not screenshots:
        lines.append("- No screenshots: the narration is your only source.")

    noted = sum(1 for e in screenshots if event_note(e))
    if noted:
        lines.append(
            f"- {noted} screenshot(s) carry a note written by the person who "
            "recorded them, shown just above the image it belongs to. Treat those "
            "as instructions about that screenshot, and do not quote them as "
            "narration or copy them into the document verbatim."
        )

    apps, dropped = _observed_applications(screenshots)
    if apps:
        lines.append(
            "- Applications and screens the operating system reported during the "
            "recording, in the order they appeared:"
        )
        lines += [f"    • {app}" for app in apps]
        if dropped:
            lines.append(f"    • (…and {dropped} more)")
        lines.append(
            "  Use these to work out which system the procedure is about and to name "
            "it in the Overview and Prerequisites. Do NOT copy them into step text as "
            "window names — they are raw title-bar text, not how a reader thinks about "
            "the screen they are looking at."
        )

    return "\n".join(lines)


def build_content_blocks(session_data: dict, session_dir: Path, config) -> list[dict]:
    """Blocks for a session: preamble, reviewed narration if any, the interleaved
    timeline, then imported photos (which have no reliable timing) at the end.

    Images are rendered per the session's mode; backends read the rendered paths.
    """
    events = session_data.get("events", [])
    edited_transcript = (session_data.get("edited_full_transcript") or "").strip()
    mode = resolve_session_mode(session_data, config)

    screenshots = sorted(
        [e for e in events if e.get("type") == "screenshot"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    blocks: list[dict] = [
        {"type": "text", "text": _session_preamble(session_data, screenshots)}
    ]

    segments = ordered_segments(session_data)

    if edited_transcript:
        # Authoritative on wording only; the timeline below repeats the same
        # narration, so the model must be told explicitly which one wins.
        blocks.append({
            "type": "text",
            "text": (
                "[FULL NARRATION (reviewed and rewritten by the person who recorded "
                "it) — THIS IS THE AUTHORITATIVE WORDING]:\n"
                f"{edited_transcript}"
            ),
        })
        if segments:
            blocks.append({
                "type": "text",
                "text": (
                    "[TIMELINE — the same narration in the order it was spoken, so you "
                    "can tell which screenshot belongs to which part of the procedure. "
                    "The wording above wins wherever the two differ; this is here for "
                    "alignment, not content, and it is NOT a second procedure.]"
                ),
            })

    timeline = sorted(
        [e for e in events
         if e.get("type") == "transcript_segment"
         or (e.get("type") == "screenshot" and not _is_imported(e))],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    for event in timeline:
        offset_s = event.get("timestamp_offset_ms", 0) / 1000
        if event.get("type") == "transcript_segment":
            text = segment_text(event)
            if text:
                blocks.append({
                    "type": "text",
                    "text": f"[NARRATION at {offset_s:.1f}s]: {text}",
                })
        else:
            blocks += _screenshot_event_blocks(event, session_dir, mode, config)

    imported = [e for e in screenshots if _is_imported(e)]
    if imported:
        blocks.append({
            "type": "text",
            "text": (
                "[PHOTOS ADDED AFTER RECORDING — these were attached by hand, in this "
                "order. They are NOT positioned in the timeline above, so use the "
                "narration to work out where in the procedure each one belongs.]"
            ),
        })
        for event in imported:
            blocks += _screenshot_event_blocks(event, session_dir, mode, config)

    return blocks
