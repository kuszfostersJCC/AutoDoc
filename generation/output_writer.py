"""
generation/output_writer.py — Write the generated markdown to the output folder.

DEPRECATED since v1.1 (PRD F2): generation/renderers.py is the output path
now — it writes md/html/docx with screenshots embedded. This module is kept
only so any straggler callers keep working; it writes plain markdown with
screenshot tokens left as-is. Do not add features here.

The output filename is derived from the document's H1 heading so the file is
human-readable (e.g., "How_to_Submit_a_Grade_Change.md"). If the title can't
be parsed, the session ID is used as a fallback.
"""

import re
from pathlib import Path


def write_output(markdown: str, session_id: str, output_dir: Path) -> Path:
    """
    Save the markdown string to output_dir and return the final file path.

    Handles duplicate filenames by appending a counter suffix.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = _derive_stem(markdown, session_id)
    output_path = _unique_path(output_dir / f"{stem}.md")
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _derive_stem(markdown: str, fallback: str) -> str:
    """Extract the H1 heading and convert it to a safe filename stem."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            # Keep alphanumeric, spaces, hyphens, underscores; collapse to underscores.
            safe = re.sub(r"[^\w\s-]", "", title)
            safe = re.sub(r"[\s]+", "_", safe.strip())
            safe = safe[:80]  # reasonable filename length cap
            if safe:
                return safe
    return fallback


def _unique_path(path: Path) -> Path:
    """If path already exists, append _1, _2, … until we find a free name."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
