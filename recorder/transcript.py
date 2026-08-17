"""
recorder/transcript.py — the narration side of session.json, in one place.

Deliberately dependency-free (stdlib only). It lives beside recorder/session.py
rather than inside it because importing that module pulls in sounddevice, pynput
and comtypes via AudioCapture/MouseHooks/ui_inspect — cost the generation path
has no business paying. generation/content.py imports this; it must not import
recorder.session.

--------------------------------------------------------------------------
TWO REPRESENTATIONS, AND WHY THE SECOND ONE IS NOW THE RARE CASE
--------------------------------------------------------------------------

A session's narration can live in either of two shapes:

  1. **Per-segment.** Each `transcript_segment` event carries Whisper's original
     `data.text` plus an optional `edited_text` override. The timeline survives,
     so generation can interleave narration with the screenshots taken at the
     same moments.
  2. **A blob.** A single `edited_full_transcript` string on the session, with no
     timestamps at all. Generation cannot interleave against it.

`edited_text` has existed on every segment since the schema was written and was
read by generation from the start — but until 2026-08-13 nothing ever WROTE it.
The session editor put every segment into one text box and saved
`toPlainText()` as a blob, unconditionally, on every save. So:

  * fixing one misheard word ("shrew" → "chew") threw away the whole timeline;
  * merely OPENING the editor and pressing Save did the same, because the box
    was pre-filled with the segments joined — no actual edit required;
  * and since the editor is also where you set the render mode, delete frames
    and draw redactions, any session that got any curation at all ended up
    blob-only. The interleaved path survived solely for sessions nobody touched.

`apply_transcript_edit()` fixes that by splitting the edited text back on blank
lines and mapping the pieces onto the segments when the counts agree, which is
the overwhelmingly common case (word-level corrections don't change how many
paragraphs there are). The blob is now reserved for a genuine restructure —
merging, splitting, or emptying paragraphs — where no honest mapping exists.

**The invariant to preserve:** `edited_full_transcript` present ⟺ the user
restructured the narration beyond per-segment mapping. Anything reading a
session may rely on that.

Nice consequence: sessions already carrying a blob from the old behaviour heal
themselves. The editor shows the blob, a mapping-preserving save maps it back
onto the segments and pops the key, and the timeline returns without a migration
step. A blob from a real restructure stays a blob, correctly.
"""

import re

# What the editor joins segments with when filling its text box, and what
# apply_transcript_edit splits on. One definition, because a round trip through
# the UI has to be lossless for the mapping to hold.
TRANSCRIPT_SEPARATOR = "\n\n"

# Runs of blank lines, however much stray whitespace they contain. Collapsing
# them (rather than treating each as a boundary) is what makes an accidentally
# double-pressed Enter a no-op instead of a mapping failure.
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def ordered_segments(session_data: dict) -> list[dict]:
    """Every transcript_segment event, in timeline order."""
    return sorted(
        [e for e in session_data.get("events", []) if e.get("type") == "transcript_segment"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )


def segment_text(event: dict) -> str:
    """
    The text of one segment: the user's edit if there is one, else Whisper's.

    `or` rather than an `is not None` check, matching every existing reader: an
    empty `edited_text` means "no edit", not "deliberately blank". That is why
    apply_transcript_edit() refuses to map empty pieces — with these semantics it
    could not represent an emptied segment, so it stores a blob instead.
    """
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
    """
    What the editor should show: the blob if a restructure produced one, else the
    segments joined. Callers get one answer instead of re-deriving the precedence.
    """
    blob = (session_data.get("edited_full_transcript") or "").strip()
    return blob if blob else transcript_from_segments(session_data)


def apply_transcript_edit(session_data: dict, edited_text: str) -> bool:
    """
    Store the editor's text on `session_data`, per-segment when possible.

    Mutates in place; the caller persists. Returns True when the edit was mapped
    onto individual segments (timeline preserved, `edited_full_transcript`
    removed) and False when it was stored as a blob.

    Mapping applies when the number of blank-line-separated pieces equals the
    number of segments. A piece identical to Whisper's original clears that
    segment's `edited_text` back to None rather than storing a redundant copy, so
    "was this segment touched?" stays answerable and a no-op save is genuinely a
    no-op.
    """
    segments = ordered_segments(session_data)
    pieces = split_transcript(edited_text)

    if segments and len(pieces) == len(segments):
        for event, piece in zip(segments, pieces):
            original = (event.get("data", {}).get("text") or "").strip()
            event["edited_text"] = None if piece == original else piece
        session_data.pop("edited_full_transcript", None)
        return True

    # No honest mapping: paragraphs were merged, split, or emptied. Keep the
    # user's text verbatim as a blob and leave the segments alone — generation
    # still uses the timeline for screenshot alignment (see content.py), it just
    # takes its wording from here.
    session_data["edited_full_transcript"] = (edited_text or "").strip()
    return False
