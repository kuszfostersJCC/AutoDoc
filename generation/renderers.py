"""
generation/renderers.py — Turn generated markdown into deliverable files:
Markdown (with image links), self-contained HTML, and Word (.docx), each with
the session's screenshots embedded at the steps they illustrate (PRD F2).

Screenshot placement contract:
The generator's system prompt instructs the model to place screenshots using
tokens of the form `{{screenshot:evt_0004}}` on their own line. This module:
  - resolves each valid token to that event's RENDERED image (respecting the
    session's render mode and redaction regions via render_event_to_cache),
  - keeps only the FIRST occurrence of each id, strips unknown/duplicate tokens,
  - appends any screenshots the model didn't place under a final
    "Screen reference" heading, so images are never silently lost — an
    imperfect model degrades to "all images at the end", not "no images".

The markdown→HTML/docx conversion is deliberately NOT a general markdown
engine. The prompt mandates a narrow structure (H1/H2/H3 headings, paragraphs,
"- " bullet lists, **bold**) and that is all this parses. If the prompt's
output format changes, update _iter_blocks() to match.

PUBLIC API — two steps, deliberately separate:

    prepared = prepare_document(markdown, session_data, session_dir, config)
    paths    = write_outputs(prepared, session_data, config, output_dir)

prepare_document() does the model-output normalization ONCE and hands back a
PreparedDocument. Every consumer of generated documentation — the file writers
here, and any publishing destination (see PRD-v1.3-bookstack.md) — must go
through it rather than re-parsing the model's raw markdown. Two reasons, both
learned the hard way:

  1. _hoist_inline_tokens() is what makes weaker models' inline token placement
     render at all. A consumer that parses raw markdown silently drops images.
  2. PreparedDocument.images is whatever render_event_to_cache() returns — the
     session's render mode, click circle, and redaction regions already applied.
     A consumer that resolves its own image paths from screenshots/ would emit
     un-redacted originals while the local .docx looked correct. (Note
     render_event_to_cache legitimately returns the source path itself when
     there is nothing to apply — raw mode with no redactions — so "always use
     what it returned" is the rule, not "never touch screenshots/".)
"""

import base64
import html as html_module
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from generation.content import resolve_session_mode
from recorder.screenshot import render_event_to_cache

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\{\{screenshot:([\w-]+)\}\}")

VALID_FORMATS = ("md", "html", "docx")


# ------------------------------------------------------------------ #
# Public entry points                                                  #
# ------------------------------------------------------------------ #

@dataclass
class PreparedDocument:
    """
    Generated documentation, normalized once and ready for any destination.

    doc     — markdown whose remaining {{screenshot:id}} tokens are guaranteed
              valid, unique, and alone on their own line.
    images  — event_id → render_event_to_cache() path, chronological. Render mode
              and redaction regions already applied. Use these paths as given;
              don't re-resolve from screenshots/.
    stem    — filesystem-safe filename stem (no extension).
    title   — the document's H1 as written, for destinations that want a human
              title rather than a filename. Empty if the model omitted an H1.
    """

    doc: str
    images: dict[str, Path] = field(default_factory=dict)
    stem: str = "session"
    title: str = ""


def prepare_document(
    markdown: str,
    session_data: dict,
    session_dir: Path,
    config,
) -> PreparedDocument:
    """
    Normalize raw model output into a PreparedDocument.

    Call this exactly once per generation and pass the result to every
    destination — see this module's docstring for why re-parsing the raw
    markdown downstream is a defect, not a shortcut.
    """
    fallback = session_data.get("session_id", "session")
    images = _resolve_images(session_data, session_dir, config)
    return PreparedDocument(
        doc=_normalize_tokens(markdown, images),
        images=images,
        # Both derived from the RAW markdown: _normalize_tokens only touches
        # screenshot tokens, but deriving from it would couple the output
        # filename to token-hoisting behaviour for no reason.
        stem=_derive_stem(markdown, fallback),
        title=_derive_title(markdown),
    )


def substitute_tokens(doc: str, resolve) -> str:
    """
    Replace every {{screenshot:id}} token via resolve(event_id) -> str.

    Public so destinations don't each carry their own copy of the token regex —
    the pattern belongs in exactly one place. Return "" from resolve to strip a
    token (used for the placeholder body BookStack needs before its images can be
    uploaded).
    """
    return _TOKEN_RE.sub(lambda m: resolve(m.group(1)), doc)


def write_outputs(
    prepared: PreparedDocument,
    session_data: dict,
    config,
    output_dir: Path,
) -> list[Path]:
    """
    Write a PreparedDocument in every configured output format.

    Returns the list of written paths (first = primary, shown in the UI).
    Raises only if NO format could be written; individual format failures
    are logged and skipped so one bad exporter can't destroy a generation
    that cost real time and tokens.

    Files land in output_dir/<session name>/ — one subfolder per session, so
    multi-format output (plus the md's images folder) doesn't pile every
    session's files into one flat directory. Re-generating the same session
    reuses its folder; filename collisions get _1/_2 suffixes as before.
    """
    session_label = session_data.get("name") or session_data.get("session_id", "session")
    output_dir = output_dir / _safe_dirname(session_label)
    output_dir.mkdir(parents=True, exist_ok=True)

    formats = [f for f in getattr(config, "output_formats", ["md"]) if f in VALID_FORMATS]
    if not formats:
        formats = ["md"]

    doc = prepared.doc
    images = prepared.images
    stem = prepared.stem

    written: list[Path] = []
    errors: list[str] = []
    for fmt in formats:
        try:
            if fmt == "md":
                written.append(_write_md(doc, images, output_dir, stem))
            elif fmt == "html":
                written.append(_write_html(doc, images, output_dir, stem))
            elif fmt == "docx":
                written.append(_write_docx(doc, images, output_dir, stem))
        except Exception as exc:
            log.exception("write_outputs: %s export failed", fmt)
            errors.append(f"{fmt}: {exc}")

    if not written:
        raise RuntimeError(
            "Every output format failed to write:\n" + "\n".join(errors)
        )
    return written


# ------------------------------------------------------------------ #
# Document preparation                                                 #
# ------------------------------------------------------------------ #

def _resolve_images(session_data: dict, session_dir: Path, config) -> dict:
    """event_id → rendered image Path, in chronological order (dicts preserve it)."""
    mode = resolve_session_mode(session_data, config)
    images: dict[str, Path] = {}
    screenshots = sorted(
        [e for e in session_data.get("events", []) if e.get("type") == "screenshot"],
        key=lambda e: e.get("timestamp_offset_ms", 0),
    )
    for event in screenshots:
        path = render_event_to_cache(session_dir, event, mode, config)
        if path is not None:
            images[event["event_id"]] = path
    return images


def _normalize_tokens(markdown: str, images: dict) -> str:
    """
    Normalize the model's token usage:
      - keep the first occurrence of each KNOWN id, drop later duplicates,
      - strip tokens for unknown ids entirely,
      - append unused screenshots under a "Screen reference" heading.
    Output is markdown whose remaining tokens are guaranteed valid and unique.
    """
    seen: set[str] = set()

    def _sub(match: re.Match) -> str:
        event_id = match.group(1)
        if event_id not in images:
            log.warning("renderers: model referenced unknown screenshot %r — stripped", event_id)
            return ""
        if event_id in seen:
            log.warning("renderers: duplicate screenshot token %r — stripped", event_id)
            return ""
        seen.add(event_id)
        return match.group(0)

    doc = _TOKEN_RE.sub(_sub, markdown)
    doc = _hoist_inline_tokens(doc)

    unused = [eid for eid in images if eid not in seen]
    if unused:
        log.info("renderers: %d screenshot(s) not placed by the model — appending", len(unused))
        lines = [doc.rstrip(), "", "## Screen reference", ""]
        for eid in unused:
            lines.append(f"{{{{screenshot:{eid}}}}}")
            lines.append("")
        doc = "\n".join(lines)
    return doc


_EMPTY_PAREN_RE = re.compile(r"\(\s*(?:see|refer to|shown in|as shown)?\s*[,;:]?\s*\)\.?", re.IGNORECASE)


def _hoist_inline_tokens(doc: str) -> str:
    """
    Move tokens embedded mid-sentence onto their own lines.

    Models — especially weaker local ones — routinely write
    "…enter the login name. (See {{screenshot:evt_0004}})." despite the
    prompt requiring tokens alone on a line. The block parser below only
    treats whole-line tokens as images, so without this pass inline tokens
    rendered as literal text in HTML/docx (observed in the field 2026-07-13,
    while the markdown writer's global regex substitution masked the problem
    in .md output). Hoisting also tidies the leftover "(See )" debris.
    """
    out: list[str] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped or _TOKEN_RE.fullmatch(stripped):
            out.append(line)
            continue
        tokens = _TOKEN_RE.findall(line)
        if not tokens:
            out.append(line)
            continue
        text = _TOKEN_RE.sub("", line)
        text = _EMPTY_PAREN_RE.sub("", text)      # "(See )." → gone
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)  # " ." → "."
        text = re.sub(r"\.{2,}", ".", text)       # ".." → "."
        text = re.sub(r"\s{2,}", " ", text).rstrip()
        if text.strip():
            out.append(text)
        # Blank lines around the token keep it its own markdown block.
        for token_id in tokens:
            out.append("")
            out.append(f"{{{{screenshot:{token_id}}}}}")
            out.append("")
    return "\n".join(out)


def _iter_blocks(doc: str):
    """
    Yield (kind, payload) blocks from the constrained markdown:
      ("heading", (level, text)) | ("bullets", [items]) | ("image", event_id)
      | ("para", text)
    """
    bullets: list[str] = []
    for raw_line in doc.splitlines():
        line = raw_line.strip()

        is_bullet = line.startswith("- ") or line.startswith("* ")
        if bullets and not is_bullet:
            yield ("bullets", bullets)
            bullets = []

        if not line:
            continue

        token = _TOKEN_RE.fullmatch(line)
        if token:
            yield ("image", token.group(1))
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            yield ("heading", (min(level, 4), line[level:].strip()))
        elif is_bullet:
            bullets.append(line[2:].strip())
        else:
            yield ("para", line)
    if bullets:
        yield ("bullets", bullets)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


# ------------------------------------------------------------------ #
# Markdown                                                             #
# ------------------------------------------------------------------ #

def _write_md(doc: str, images: dict, output_dir: Path, stem: str) -> Path:
    path = _unique_path(output_dir / f"{stem}.md")
    final_stem = path.stem  # may have _1 suffix; keep image dir in sync

    if images:
        img_dir = output_dir / f"{final_stem}_images"
        img_dir.mkdir(exist_ok=True)

        def _sub(match: re.Match) -> str:
            eid = match.group(1)
            src = images[eid]
            dest = img_dir / f"{eid}{src.suffix}"
            shutil.copy2(src, dest)
            return f"![Screenshot {eid}]({img_dir.name}/{dest.name})"

        doc = _TOKEN_RE.sub(_sub, doc)

    path.write_text(doc, encoding="utf-8")
    return path


# ------------------------------------------------------------------ #
# Self-contained HTML                                                  #
# ------------------------------------------------------------------ #

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: "Segoe UI", system-ui, sans-serif; line-height: 1.6;
         max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }}
  h1 {{ border-bottom: 2px solid #2563eb; padding-bottom: .3rem; }}
  h2 {{ margin-top: 2rem; color: #1e40af; }}
  h3 {{ margin-top: 1.5rem; }}
  img {{ max-width: 100%; border: 1px solid #d1d5db; border-radius: 6px;
        box-shadow: 0 1px 3px rgba(0,0,0,.1); margin: .5rem 0 1rem; }}
  ul {{ padding-left: 1.4rem; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _write_html(doc: str, images: dict, output_dir: Path, stem: str) -> Path:
    title = stem.replace("_", " ")
    parts: list[str] = []
    for kind, payload in _iter_blocks(doc):
        if kind == "heading":
            level, text = payload
            if level == 1:
                title = text
            parts.append(f"<h{level}>{_inline_html(text)}</h{level}>")
        elif kind == "bullets":
            items = "".join(f"<li>{_inline_html(i)}</li>" for i in payload)
            parts.append(f"<ul>{items}</ul>")
        elif kind == "image":
            src = images[payload]
            b64 = base64.standard_b64encode(src.read_bytes()).decode("ascii")
            parts.append(f'<img src="data:image/png;base64,{b64}" alt="Screenshot {payload}">')
        else:
            parts.append(f"<p>{_inline_html(payload)}</p>")

    path = _unique_path(output_dir / f"{stem}.html")
    path.write_text(
        _HTML_SHELL.format(title=html_module.escape(title), body="\n".join(parts)),
        encoding="utf-8",
    )
    return path


def _inline_html(text: str) -> str:
    escaped = html_module.escape(text)
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


# ------------------------------------------------------------------ #
# Word (.docx)                                                         #
# ------------------------------------------------------------------ #

def _write_docx(doc: str, images: dict, output_dir: Path, stem: str) -> Path:
    # Imported here so a broken python-docx install only breaks docx export,
    # not md/html (write_outputs skips failed formats).
    from docx import Document
    from docx.shared import Inches

    document = Document()
    for kind, payload in _iter_blocks(doc):
        if kind == "heading":
            level, text = payload
            # python-docx: level 0 = Title style; map H1→Title-ish heading 0.
            document.add_heading(_strip_bold(text), level=max(0, level - 1))
        elif kind == "bullets":
            for item in payload:
                document.add_paragraph(_strip_bold(item), style="List Bullet")
        elif kind == "image":
            document.add_picture(str(images[payload]), width=Inches(6.0))
        else:
            _add_para_with_bold(document, payload)

    path = _unique_path(output_dir / f"{stem}.docx")
    document.save(str(path))
    return path


def _strip_bold(text: str) -> str:
    return _BOLD_RE.sub(r"\1", text)


def _add_para_with_bold(document, text: str) -> None:
    """Paragraph preserving **bold** spans as bold runs."""
    para = document.add_paragraph()
    pos = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > pos:
            para.add_run(text[pos:match.start()])
        para.add_run(match.group(1)).bold = True
        pos = match.end()
    if pos < len(text):
        para.add_run(text[pos:])


# ------------------------------------------------------------------ #
# Filename helpers (moved from output_writer, semantics unchanged)      #
# ------------------------------------------------------------------ #

def _safe_dirname(name: str) -> str:
    """Session name → filesystem-safe output folder name."""
    safe = re.sub(r"[^\w\s-]", "", name)
    safe = re.sub(r"\s+", "_", safe.strip())[:60]
    return safe or "session"


def _iter_h1_titles(markdown: str):
    """Yield each H1's text, in document order."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            yield stripped[2:].strip()


def _derive_title(markdown: str) -> str:
    """
    The document's first H1 exactly as the model wrote it, or "" if there is none.

    Kept separate from _derive_stem because a filename stem is not a title:
    "Reset a Locked Account" becomes "Reset_a_Locked_Account" as a stem, which is
    correct on disk and wrong as a page heading anywhere else.
    """
    return next((t for t in _iter_h1_titles(markdown) if t), "")


def _derive_stem(markdown: str, fallback: str) -> str:
    """Extract the H1 heading and convert it to a safe filename stem."""
    # Keeps the original semantics: an H1 that sanitizes to nothing (e.g. one
    # made entirely of punctuation) falls through to the next H1, not to fallback.
    for title in _iter_h1_titles(markdown):
        safe = re.sub(r"[^\w\s-]", "", title)
        safe = re.sub(r"[\s]+", "_", safe.strip())
        safe = safe[:80]
        if safe:
            return safe
    return fallback


def _unique_path(path: Path) -> Path:
    """If path already exists, append _1, _2, … until we find a free name."""
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.parent / f"{path.stem}_{counter}{path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
