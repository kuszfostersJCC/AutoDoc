"""Renders a prepared document to .md, self-contained .html and .docx.

_iter_blocks() must parse exactly the markdown subset generation/content.py's prompt
permits; anything wider reaches the reader as raw pipes and asterisks.
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


@dataclass
class PreparedDocument:
    """Generated documentation, normalized once and ready for any destination.

    `doc`'s remaining tokens are valid, unique and alone on their line. `images`
    paths already have render mode and redactions applied — use them as given
    rather than re-resolving from screenshots/.
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
    """Normalize raw model output once per generation; every destination uses the result."""
    fallback = session_data.get("session_id", "session")
    images = _resolve_images(session_data, session_dir, config)
    return PreparedDocument(
        doc=_normalize_tokens(markdown, images),
        images=images,
        # From the RAW markdown, so the filename can't depend on token hoisting.
        stem=_derive_stem(markdown, fallback),
        title=_derive_title(markdown),
    )


def substitute_tokens(doc: str, resolve) -> str:
    """Replace every {{screenshot:id}} token via resolve(event_id) -> str.

    Returning "" from resolve strips the token.
    """
    return _TOKEN_RE.sub(lambda m: resolve(m.group(1)), doc)


def write_outputs(
    prepared: PreparedDocument,
    session_data: dict,
    config,
    output_dir: Path,
) -> list[Path]:
    """Write a PreparedDocument into output_dir/<session name>/, one format each.

    Returns the written paths (first = primary). Individual format failures are
    logged and skipped; raises only when every format failed.
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


# --- Document preparation ---

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
    """Keep the first occurrence of each known id, strip unknown and duplicate tokens,
    and append screenshots the model never placed under a "Screen reference" heading.
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
    """Move tokens embedded mid-sentence onto their own lines.

    Models write them inline despite the prompt, and _iter_blocks() only treats a
    whole-line token as an image.
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
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        text = re.sub(r"\.{2,}", ".", text)
        text = re.sub(r"\s{2,}", " ", text).rstrip()
        if text.strip():
            out.append(text)
        # Blank lines around the token keep it its own markdown block.
        for token_id in tokens:
            out.append("")
            out.append(f"{{{{screenshot:{token_id}}}}}")
            out.append("")
    return "\n".join(out)


# --- Block parsing ---

# The required whitespace after the marker keeps "**Note**" from matching.
_UL_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)[-*+][ \t]+(?P<text>.*)$")
_OL_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)\d{1,3}[.)][ \t]+(?P<text>.*)$")
# Rules and setext underlines, dropped rather than rendered: a literal row of
# dashes in a Word document is worse than a missing separator.
_RULE_RE = re.compile(r"^[-*_=]{3,}$")


def _indent_width(indent: str) -> int:
    """Tabs count as 4. Only relative order matters — see _assign_levels."""
    return len(indent.replace("\t", "    "))


def _match_list_item(raw_line: str) -> dict | None:
    """A list item as {"indent": int, "ordered": bool, "text": str}, or None."""
    for regex, ordered in ((_UL_ITEM_RE, False), (_OL_ITEM_RE, True)):
        match = regex.match(raw_line)
        if match:
            return {
                "indent": _indent_width(match.group("indent")),
                "ordered": ordered,
                "text": match.group("text").strip(),
            }
    return None


def _assign_levels(items: list[dict]) -> list[dict]:
    """Turn raw indent widths into 0-based nesting levels.

    Only relative indent matters, because models mix 2-, 3- and 4-space nesting
    inside one list.
    """
    stack: list[int] = []
    out: list[dict] = []
    for item in items:
        indent = item["indent"]
        while stack and indent < stack[-1]:
            stack.pop()
        if not stack or indent > stack[-1]:
            stack.append(indent)
        out.append({
            "level": len(stack) - 1,
            "ordered": item["ordered"],
            "text": item["text"],
        })
    return out


def _is_table_delimiter(line: str) -> bool:
    """True for a pipe-table separator row like `|---|:--:|`."""
    stripped = line.strip()
    if "|" not in stripped or "-" not in stripped:
        return False
    return all(char in "|-: \t" for char in stripped)


def _split_row(line: str) -> list[str]:
    """Split one pipe-table row, tolerating missing outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _parse_alignments(delimiter: str, width: int) -> list[str | None]:
    """Column alignments from the separator row: `:--`, `:-:`, `--:`."""
    aligns: list[str | None] = []
    for cell in _split_row(delimiter):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append(
            "center" if left and right else "right" if right else "left" if left else None
        )
    return (aligns + [None] * width)[:width]


def _try_parse_table(lines: list[str], start: int) -> tuple[dict, int] | None:
    """Parse a pipe table beginning at `start`, returning (block, lines_consumed) or None.

    The separator row is required, or a sentence containing a pipe becomes a table;
    rows are padded or truncated to the header width.
    """
    if start + 1 >= len(lines):
        return None
    header_line = lines[start].strip()
    if "|" not in header_line or not _is_table_delimiter(lines[start + 1]):
        return None

    header = _split_row(header_line)
    aligns = _parse_alignments(lines[start + 1], len(header))

    rows: list[list[str]] = []
    index = start + 2
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or "|" not in stripped:
            break
        cells = _split_row(stripped)
        rows.append((cells + [""] * len(header))[: len(header)])
        index += 1

    return {"header": header, "align": aligns, "rows": rows}, index - start


def _next_content_is_list_item(lines: list[str], start: int) -> bool:
    """Looking past blank lines, is the next real line another list item?"""
    for line in lines[start:]:
        if line.strip():
            return _match_list_item(line) is not None
    return False


def _iter_blocks(doc: str):
    """Yield (kind, payload) blocks from the model's markdown:

      ("heading", (level, text))
      ("list",    [{"level": int, "ordered": bool, "text": str}, ...])
      ("table",   {"header": [str], "align": [str|None], "rows": [[str], ...]})
      ("image",   event_id)
      ("para",    text)

    Not a general markdown engine: fenced code, block quotes and HTML are not parsed.
    """
    lines = doc.splitlines()
    index = 0
    pending: list[dict] = []

    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()

        if not line:
            # A blank line between items is a loose list, not the end of one.
            if pending and _next_content_is_list_item(lines, index + 1):
                index += 1
                continue
            if pending:
                yield ("list", _assign_levels(pending))
                pending = []
            index += 1
            continue

        table = _try_parse_table(lines, index)
        if table is not None:
            if pending:
                yield ("list", _assign_levels(pending))
                pending = []
            block, consumed = table
            yield ("table", block)
            index += consumed
            continue

        item = _match_list_item(raw_line)
        if item is not None:
            pending.append(item)
            index += 1
            continue

        # Anything else ends an open list.
        if pending:
            yield ("list", _assign_levels(pending))
            pending = []

        token = _TOKEN_RE.fullmatch(line)
        if token:
            yield ("image", token.group(1))
        elif _RULE_RE.fullmatch(line):
            pass
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            yield ("heading", (min(level, 4), line[level:].strip()))
        else:
            yield ("para", line)
        index += 1

    if pending:
        yield ("list", _assign_levels(pending))


# --- Inline formatting ---

# Bold BEFORE italic so `**x**` isn't consumed by the single-asterisk branch.
# Underscore emphasis is unsupported on purpose: it would eat the underscores in
# field names like some_field_name.
_INLINE_RE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|(?<!\*)\*(?P<italic>[^*\n]+?)\*(?!\*)"
    r"|`(?P<code>[^`\n]+?)`"
)


def _iter_inline(text: str):
    """Yield (chunk, bold, italic, code) spans covering `text` exactly once."""
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            yield (text[position:match.start()], False, False, False)
        if match.group("bold") is not None:
            yield (match.group("bold"), True, False, False)
        elif match.group("italic") is not None:
            yield (match.group("italic"), False, True, False)
        else:
            yield (match.group("code"), False, False, True)
        position = match.end()
    if position < len(text):
        yield (text[position:], False, False, False)


def _strip_inline(text: str) -> str:
    """Text with emphasis markers removed — for headings, which style themselves."""
    return "".join(chunk for chunk, _b, _i, _c in _iter_inline(text))


# --- Markdown ---

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


# --- Self-contained HTML ---

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
  ul, ol {{ padding-left: 1.4rem; }}
  ul ul, ul ol, ol ol, ol ul {{ margin: .2rem 0; }}
  code {{ background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 3px;
         padding: .05rem .3rem; font-size: .92em;
         font-family: Consolas, "Courier New", monospace; }}
  table {{ border-collapse: collapse; margin: .75rem 0 1.25rem; width: 100%; }}
  th, td {{ border: 1px solid #d1d5db; padding: .4rem .6rem; text-align: left;
           vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _list_html(items: list[dict]) -> str:
    """Nested <ul>/<ol> from the flat level-tagged items.

    A sublist is emitted as a sibling of its parent <li>, not inside it; browsers
    indent both identically.
    """
    parts: list[str] = []
    open_tags: list[str] = []
    for item in items:
        depth = item["level"] + 1
        tag = "ol" if item["ordered"] else "ul"
        while len(open_tags) > depth:
            parts.append(f"</{open_tags.pop()}>")
        # Same depth but switched between numbered and bulleted: close and reopen.
        if len(open_tags) == depth and open_tags[-1] != tag:
            parts.append(f"</{open_tags.pop()}>")
        while len(open_tags) < depth:
            parts.append(f"<{tag}>")
            open_tags.append(tag)
        parts.append(f"<li>{_inline_html(item['text'])}</li>")
    while open_tags:
        parts.append(f"</{open_tags.pop()}>")
    return "".join(parts)


def _table_html(block: dict) -> str:
    def cell(tag: str, text: str, align: str | None) -> str:
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{_inline_html(text)}</{tag}>"

    aligns = block["align"]
    head = "".join(cell("th", t, aligns[i]) for i, t in enumerate(block["header"]))
    body = "".join(
        "<tr>" + "".join(cell("td", t, aligns[i]) for i, t in enumerate(row)) + "</tr>"
        for row in block["rows"]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _write_html(doc: str, images: dict, output_dir: Path, stem: str) -> Path:
    title = stem.replace("_", " ")
    parts: list[str] = []
    for kind, payload in _iter_blocks(doc):
        if kind == "heading":
            level, text = payload
            if level == 1:
                title = text
            parts.append(f"<h{level}>{_inline_html(text)}</h{level}>")
        elif kind == "list":
            parts.append(_list_html(payload))
        elif kind == "table":
            parts.append(_table_html(payload))
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
    out: list[str] = []
    for chunk, bold, italic, code in _iter_inline(text):
        escaped = html_module.escape(chunk)
        if code:
            out.append(f"<code>{escaped}</code>")
        elif bold:
            out.append(f"<strong>{escaped}</strong>")
        elif italic:
            out.append(f"<em>{escaped}</em>")
        else:
            out.append(escaped)
    return "".join(out)


# --- Word (.docx) ---

# Deepest built-in "List Bullet N" style in python-docx's default template.
_DOCX_MAX_LIST_LEVEL = 2
# Word's default body indent step.
_DOCX_INDENT_STEP_IN = 0.25


def _write_docx(doc: str, images: dict, output_dir: Path, stem: str) -> Path:
    # Imported here so a broken python-docx install only breaks docx export.
    from docx import Document
    from docx.shared import Inches

    document = Document()
    for kind, payload in _iter_blocks(doc):
        if kind == "heading":
            level, text = payload
            # python-docx: level 0 = Title style; map H1→Title-ish heading 0.
            document.add_heading(_strip_inline(text), level=max(0, level - 1))
        elif kind == "list":
            _add_docx_list(document, payload)
        elif kind == "table":
            _add_docx_table(document, payload)
        elif kind == "image":
            document.add_picture(str(images[payload]), width=Inches(6.0))
        else:
            _add_docx_runs(document.add_paragraph(), payload)

    path = _unique_path(output_dir / f"{stem}.docx")
    document.save(str(path))
    return path


def _add_docx_runs(para, text: str, force_bold: bool = False) -> None:
    """Fill a paragraph with runs carrying bold / italic / monospace."""
    for chunk, bold, italic, code in _iter_inline(text):
        run = para.add_run(chunk)
        run.bold = bold or force_bold
        run.italic = italic
        if code:
            run.font.name = "Consolas"


def _add_docx_list(document, items: list[dict]) -> None:
    """Write a nested list.

    Numbered items are literal text plus an indent, not Word's "List Number" style:
    that style shares one numbering definition per document, so a later list would
    continue counting instead of restarting at 1.
    """
    from docx.shared import Inches

    counters: dict[int, int] = {}
    for item in items:
        level = min(item["level"], _DOCX_MAX_LIST_LEVEL)
        # Drop counters below this level so the next sublist restarts at 1.
        for deeper in [key for key in counters if key > level]:
            del counters[deeper]

        if item["ordered"]:
            counters[level] = counters.get(level, 0) + 1
            para = document.add_paragraph()
            para.paragraph_format.left_indent = Inches(
                _DOCX_INDENT_STEP_IN * (level + 1)
            )
            para.add_run(f"{counters[level]}. ")
            _add_docx_runs(para, item["text"])
        else:
            counters.pop(level, None)
            style = "List Bullet" if level == 0 else f"List Bullet {level + 1}"
            try:
                para = document.add_paragraph(style=style)
            except KeyError:
                # Template without the deeper styles: fake the depth with an indent.
                para = document.add_paragraph(style="List Bullet")
                para.paragraph_format.left_indent = Inches(
                    _DOCX_INDENT_STEP_IN * (level + 1)
                )
            _add_docx_runs(para, item["text"])


def _add_docx_table(document, block: dict) -> None:
    """Write a pipe table as a real Word table with a bold header row."""
    header = block["header"]
    table = document.add_table(rows=1, cols=len(header))
    try:
        # Without a border style a Word table has no visible gridlines.
        table.style = "Table Grid"
    except KeyError:
        pass
    table.autofit = True

    for index, text in enumerate(header):
        cell_para = table.rows[0].cells[index].paragraphs[0]
        _add_docx_runs(cell_para, text, force_bold=True)
        _align_docx_para(cell_para, block["align"][index])

    for row in block["rows"]:
        cells = table.add_row().cells
        for index, text in enumerate(row):
            cell_para = cells[index].paragraphs[0]
            _add_docx_runs(cell_para, text)
            _align_docx_para(cell_para, block["align"][index])


def _align_docx_para(para, align: str | None) -> None:
    """Apply a markdown column alignment. Silent no-op if unavailable."""
    if not align or align == "left":
        return
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        para.alignment = {
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
        }[align]
    except (ImportError, KeyError):
        pass


# --- Filename helpers ---

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
    """The document's first H1 exactly as the model wrote it, or "" if there is none."""
    return next((t for t in _iter_h1_titles(markdown) if t), "")


def _derive_stem(markdown: str, fallback: str) -> str:
    """Extract the H1 heading and convert it to a safe filename stem."""
    # An H1 that sanitizes to nothing falls through to the next H1, not to fallback.
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
