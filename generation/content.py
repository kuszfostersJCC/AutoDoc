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

--------------------------------------------------------------------------
NARRATION AND SCREENSHOTS ARE INTERLEAVED — ALWAYS (fixed 2026-08-13)
--------------------------------------------------------------------------

Screenshots are emitted at their place in the timeline, beside the narration
spoken while they were taken. That pairing is most of what makes a 25-step
recording tractable: the model gets "here is what was said, here is the window it
was said about" rather than a wall of text followed by a wall of images it has to
re-align from timestamps itself.

It did not always work that way. Whenever `edited_full_transcript` was present
this module emitted the whole blob first and every screenshot after it — and the
session editor wrote that blob on EVERY save, so any session anyone had curated
at all took the degraded path. Fixing the editor (see recorder/transcript.py)
made per-segment edits the normal case, and the blob now only appears after a
genuine restructure. When it does appear, it is emitted first as the authoritative
wording AND the segment timeline is still interleaved with the screenshots for
alignment, with the model told explicitly which of the two to trust on wording.
The narration is duplicated on that path: a few percent of a payload dominated by
images, which is a bargain for keeping the alignment.

Imported photos (`click_type == "imported"`) are the one thing with genuinely no
timeline position — recorder/session.py::import_screenshots() spaces them 1s after
the last event, so their offsets are ordering, not timing. They are grouped at the
end under their own heading instead of interleaved, which is the part the old
blob-first shape was actually right about.

--------------------------------------------------------------------------
THE SESSION PREAMBLE USES ONLY WHAT WAS ALREADY CAPTURED
--------------------------------------------------------------------------

`_session_preamble()` hands the model the session name, duration, screenshot
count, and the applications and window titles UI Automation already recorded at
click time. Every bit of that was being collected before this change — the name
comes from the dialog shown when a recording stops — and it feeds the two sections
the model otherwise has to invent from narration alone (Overview, Prerequisites).

Do NOT extend this by asking the user for more (audience, purpose, document type).
Almost nobody at the institution writes documentation today, so the binding risk
is people deciding the tool costs more than it returns, and a new required field
at rollout buys prompt quality with the scarcest thing available. Harvest what
recording already knows.

--------------------------------------------------------------------------
CLICK-TARGET GROUND TRUTH IS TIERED, NOT ABSOLUTE (fixed 2026-08-13)
--------------------------------------------------------------------------

UI Automation's `ElementFromPoint` returns the DEEPEST element under the cursor,
which in a browser is very often a container or a run of text rather than the
control the person meant to click. The captured name is then not a label at all.
Observed in a real generated document, all three traceable to the same cause:

    "click the pane titled **1 of 2**"                 (control type: pane —
                                                        a carousel indicator)
    "click the document titled **Sign in to your       (control type: document —
     account**"                                        name is the PAGE TITLE)
    "click on the text **Hello, Foster Kusz!**"        (control type: text —
                                                        and it leaked a real name)

None of that was the model inventing things. The prompt used to say *"Treat these
element names as authoritative … never contradict a ground-truth label based on
what you think you see in the image"*, so the model was obeying instructions. The
fix belongs in the harness, not in a bigger model: `_describe_click()` now judges
each capture and tells the model which of two situations it is in.

  * **Actionable role + usable name** (`_ACTIONABLE_ROLES`, `_label_is_unusable`)
    → presented as reliable, with an instruction to reuse it verbatim. This is
    the case worth having; it is why the click-target capture exists.
  * **Anything else** → stated explicitly as "no named control could be
    identified", naming the container type so the model understands why, and
    sending it to the screenshot. Any captured text is still shown, but labelled
    as context for understanding and explicitly not to be quoted.

Judgement moved from the model into the harness on purpose. A 4B-class local
model will follow "this name is reliable / this one is not" far more dependably
than it will apply a rule about when to distrust something.

**Window titles are captured at click time, so on any click that navigates they
name the page being LEFT.** That is how a document came to say "click Home in the
New Incognito Tab window". They are now labelled "window before this click", run
through `clean_window_title()` to drop "- Google Chrome" style decoration, and
the prompt forbids putting title-bar text into step text at all.

Raw capture is left alone: `recorder/ui_inspect.py` still records whatever UIA
said, and all of this judgement happens at prompt-build time. Same principle as
storing screenshots raw and rendering on demand — the heuristics here will need
tuning, and that must not require re-recording anything.
"""

import re
from pathlib import Path

from recorder.screenshot import (
    event_is_legacy,
    render_event_to_cache,
    resolve_render_mode,
)
from recorder.transcript import ordered_segments, segment_text

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


# --------------------------------------------------------------------- #
# Click-target ground truth, tiered by how much it can be trusted        #
# (2026-08-13 — see the module docstring section below)                  #
# --------------------------------------------------------------------- #

# UIA control types that name something a reader can actually look for and
# click. Only these get presented to the model as a control name to reuse.
_ACTIONABLE_ROLES = {
    "button", "checkbox", "combo box", "link", "list item", "menu item",
    "radio button", "split button", "tab", "tab item", "text field",
    "tree item", "slider", "spinner", "menu", "toolbar", "header item",
    "data item",
}

# Names UIA reports that are never a control label a person would recognise.
# "1 of 2" is a carousel/position indicator; the pattern shows up constantly on
# marketing pages and is what produced "click the pane titled 1 of 2".
_POSITION_NAME_RE = re.compile(r"^\d+\s*(of|/)\s*\d+$", re.IGNORECASE)

# Beyond this, the "name" is a sentence or a paragraph of page text, not a
# label. recorder/ui_inspect.py truncates at 120 chars, which is far past the
# length of any real control label.
_MAX_LABEL_CHARS = 60

# Window-manager decoration on a title bar. Stripping it matters twice: the
# model quotes these verbatim into step text if it sees them, and the preamble's
# application list reads as noise with them attached.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*("
    r"Google Chrome|Chromium|Mozilla Firefox|Microsoft. Edge|Microsoft Edge"
    r"|Brave|Opera|Safari|Internet Explorer"
    r")\s*$",
    re.IGNORECASE,
)
# Edge appends a profile segment before the browser name ("… - Work - Microsoft
# Edge"), so one pass can leave "… - Work" behind. Two passes is enough for
# every real title; a loop risks eating a legitimate trailing segment.
_TITLE_PROFILE_RE = re.compile(r"\s*[-–—]\s*(Work|Personal|Profile \d+)\s*$", re.IGNORECASE)


def clean_window_title(title: str) -> str:
    """
    A window title with browser/profile decoration removed.

    "Home Page - Jackson College - Google Chrome" → "Home Page - Jackson College"

    Public because the preamble's application list wants the same treatment.
    Returns "" unchanged; never raises.
    """
    cleaned = (title or "").strip()
    cleaned = _TITLE_SUFFIX_RE.sub("", cleaned)
    cleaned = _TITLE_PROFILE_RE.sub("", cleaned)
    return cleaned.strip()


def _label_is_unusable(name: str) -> bool:
    """
    True when a UIA element name cannot honestly be called a control label.

    Both cases were observed being copied verbatim into a finished document:

      * a position indicator ("1 of 2") — a carousel's accessible name, which
        produced "click the pane titled 1 of 2";
      * a whole sentence or multi-line run — page prose, not a control label.

    NOT checked here: "the name equals the page title", which is the shape of the
    third observed failure ("click the document titled Sign in to your account").
    That one is a `document`-node capture and is already excluded by the role
    check in _describe_click, so testing the name as well only adds false
    positives — a button labelled "Sign in" on a page titled "Sign In" is
    perfectly legitimate and was being rejected. Let the role decide whether UIA
    found a control; let this decide whether the name reads like a label.
    """
    name = (name or "").strip()
    if not name:
        return True
    if _POSITION_NAME_RE.match(name):
        return True
    return "\n" in name or len(name) > _MAX_LABEL_CHARS


def _describe_click(event: dict) -> str:
    """
    Human-readable description of the interaction behind a screenshot event.

    The clicked element captured at click time (data.ui_element, PRD F1) is the
    most reliable signal in the pipeline WHEN IT LANDS ON A REAL CONTROL, and
    misleading garbage when it doesn't. This function decides which of those
    happened and says so, rather than handing everything to the model under one
    "ground truth" banner — see the module docstring for what that cost.

    Three outcomes:
      * a named, actionable control  → present it and invite verbatim reuse;
      * UIA answered with a container or unusable name → say explicitly that
        there is no reliable name, and send the model to the screenshot;
      * nothing captured at all → the click position only, as before.
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
        # Say WHY it's unreliable. Naming the container type is the difference
        # between the model ignoring the hint and the model writing "click the
        # pane titled 1 of 2".
        container = role or "an unnamed element"
        detail = f"it reported {container}" if role else "it reported nothing usable"
        desc += (
            f" — the operating system could NOT identify a named control here "
            f"({detail}), which happens when a click lands on a container or on "
            f"plain text rather than a button. Work out what was clicked from the "
            f"screenshot and describe it in your own words."
        )
        if name and not _POSITION_NAME_RE.match(name):
            # Worth showing anyway: it is usually the text near or inside what was
            # clicked, which helps the model work out WHAT happened. Framed as
            # context-for-understanding rather than a name, because the model
            # will quote anything presented as a name — and this text often
            # contains whoever was signed in ("Hello, Jane Smith!").
            snippet = name if len(name) <= _MAX_LABEL_CHARS else name[:_MAX_LABEL_CHARS] + "…"
            desc += (
                f" (text found at that spot, to help you understand what was "
                f"clicked — NOT a control name and NOT to be quoted: \"{snippet}\")"
            )

    cleaned_title = clean_window_title(window_title)
    if cleaned_title:
        # Explicitly BEFORE the click. Titles are captured at click time, so on
        # any click that navigates, this names the page being left — which is
        # why the document once said "click Home in the New Incognito Tab
        # window". The prompt forbids quoting it into step text.
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


def _is_imported(event: dict) -> bool:
    """A photo added after recording — its timestamp is ordering, not timing."""
    return event.get("data", {}).get("click_type") == "imported"


def _format_duration(session_data: dict) -> str:
    """
    Recording length as "4m 12s", from the last event's offset.

    Derived from the event timeline rather than started_at/stopped_at because
    those are wall-clock and include the pause the user took to answer the phone,
    which is not a fact about the procedure.
    """
    events = session_data.get("events", [])
    last_ms = max((e.get("timestamp_offset_ms", 0) for e in events), default=0)
    total_s = int(last_ms / 1000)
    return f"{total_s // 60}m {total_s % 60:02d}s"


# Enough to show the shape of the journey without letting a 30-screenshot
# session turn the preamble into a page of window titles.
_MAX_OBSERVED_APPS = 12


def _observed_applications(screenshots: list[dict]) -> tuple[list[str], int]:
    """
    Distinct "app — window title" pairs from the UIA capture, in first-seen order.

    Returns (labels, how_many_were_dropped).

    This is the highest-value thing in the preamble: it's the only place the model
    learns what SYSTEM the procedure is about. The narration usually doesn't say
    ("then I go to the student record"), because the person recording can see the
    screen and doesn't think to name it.

    Titles go through clean_window_title() — a list full of "- Google Chrome"
    reads as noise, and the model copies what it is shown.
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
    """
    Context the recording already knows, as one leading text block.

    Nothing here asks anything of the user — see the module docstring on why that
    constraint is deliberate rather than lazy.
    """
    lines = ["[SESSION CONTEXT — background, not narration. Do not turn these into steps.]"]

    name = (session_data.get("name") or "").strip()
    if name:
        # The name the person typed when they stopped recording. Often a better
        # statement of intent than anything in the narration, which tends to
        # start mid-thought — but it is a hint, not a mandate, because people
        # also type "test 3".
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
    """
    Build the full list of neutral content blocks for a session. Screenshot
    images are rendered (blur/circle/raw per the session's mode) into
    session_dir/rendered/ and the rendered paths are what backends read.

    Layout, in order:
      1. the session preamble (name, duration, counts, observed applications);
      2. the reviewed full narration, IF a restructuring edit produced one;
      3. the timeline: narration segments and the screenshots taken during them,
         interleaved by timestamp;
      4. imported photos, grouped at the end — they have no real timing.

    Sessions with no screenshots (transcript-only recordings) fall out of this
    naturally: the returned list simply contains no image blocks.
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
        # A restructured transcript: authoritative on WORDING, useless for
        # alignment. Say both things explicitly — the timeline below repeats this
        # narration in its original segmentation, and without being told which to
        # prefer the model treats the repetition as two different sources and
        # sometimes documents the procedure twice.
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

    # The timeline proper. Imported photos are excluded here and appended below.
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
