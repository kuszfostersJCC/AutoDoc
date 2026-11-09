"""Narration accessors for session.json. Stdlib only: generation imports this
rather than recorder.session, which drags in sounddevice/pynput/comtypes.
"""

import re

# What the editor joins segments with, and what apply_transcript_edit splits on.
TRANSCRIPT_SEPARATOR = "\n\n"

# Runs of blank lines collapse, so a double-pressed Enter is not a boundary.
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def ordered_segments(session_data: dict) -> list[dict]:
    """Every transcript_segment event, in timeline order."""
    return sorted(
        [e for e in session_data.get("events", []) if e.get("type") == "transcript_segment"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )


def segment_text(event: dict) -> str:
    """One segment's text: the user's edit if there is one, else Whisper's. An empty
    `edited_text` means "no edit", not "deliberately blank"."""
    return ((event.get("edited_text") or event.get("data", {}).get("text") or "")).strip()


def split_transcript(text: str) -> list[str]:
    """Editor text → the pieces a per-segment mapping would use. Empties dropped."""
    return [piece.strip() for piece in _BLANK_LINE_RE.split(text or "") if piece.strip()]


def transcript_from_segments(session_data: dict) -> str:
    """The segment timeline rendered as one editable string."""
    return TRANSCRIPT_SEPARATOR.join(
        t for t in (segment_text(e) for e in ordered_segments(session_data)) if t
    )


def editable_transcript(session_data: dict) -> str:
    """What the editor shows: the restructure blob if there is one, else the segments
    joined — the one place that precedence is spelled out."""
    blob = (session_data.get("edited_full_transcript") or "").strip()
    return blob if blob else transcript_from_segments(session_data)


def apply_transcript_edit(session_data: dict, edited_text: str) -> bool:
    """Store the editor's text per-segment when the piece count matches, else as an
    `edited_full_transcript` blob. Mutates in place; True = timeline preserved."""
    segments = ordered_segments(session_data)
    pieces = split_transcript(edited_text)

    if segments and len(pieces) == len(segments):
        for event, piece in zip(segments, pieces):
            original = (event.get("data", {}).get("text") or "").strip()
            event["edited_text"] = None if piece == original else piece
        session_data.pop("edited_full_transcript", None)
        return True

    # No honest mapping (paragraphs merged, split or emptied): keep a blob.
    session_data["edited_full_transcript"] = (edited_text or "").strip()
    return False
