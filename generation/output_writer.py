"""Writes plain markdown, screenshot tokens left as-is.

Deprecated; generation/renderers.py is the current output path.
"""

import re
from pathlib import Path


def write_output(markdown: str, session_id: str, output_dir: Path) -> Path:
    """Save the markdown to output_dir and return the final path."""
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = _derive_stem(markdown, session_id)
    output_path = _unique_path(output_dir / f"{stem}.md")
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def _derive_stem(markdown: str, fallback: str) -> str:
    """Extract the H1 heading and convert it to a safe filename stem."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
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
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
