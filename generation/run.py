"""
generation/run.py — the ONE generate-then-write sequence.

Every "Generate documentation" button in the app funnels through
run_generation(). Before this module existed the sequence was implemented twice
— once in sessions_window.py::_do_generate() and once in a nested closure inside
session_editor.py::_run_generation_inline() — and the two copies had already
drifted on which one re-read session.json from disk, which one enforced the
concurrency guard, and how they revealed the finished file. Adding a third step
(BookStack publishing, see PRD-v1.3-bookstack.md) to both copies would have
guaranteed they disagreed about who records the page id and who owns a
half-finished publish.

If you add a step to the pipeline, it goes here, once.

THREADING: this function does blocking network and disk work and MUST be called
from a worker thread, never the Qt main thread. It touches no widgets and knows
nothing about the UI, so callers own all progress reporting — via
ui_service.call_soon() only (invariant #1 in HANDOFF.md).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import AutoDocConfig, get_app_dir
from generation.renderers import PreparedDocument
from publish.bookstack_publisher import PublishResult, publish_session

log = logging.getLogger(__name__)

# Prepared markdown cached inside the session folder so a failed publish can be
# retried without regenerating. Token form, not the rendered output.
MARKDOWN_CACHE = "last_generated.md"


@dataclass
class GenerationResult:
    """
    Everything a caller (or a downstream publishing step) could want from a run.

    paths     — written files, first = primary (what the UI offers to open).
    prepared  — the normalized document: markdown with valid screenshot tokens,
                plus the RENDERED image for each. Publishing destinations must
                use this rather than re-reading the .md or walking screenshots/;
                see generation/renderers.py's module docstring.
    session_data — the session.json contents this run used, as loaded from disk.
    publish   — outcome of the BookStack step, or None if it never ran. Check
                `.skipped` before reporting anything: skipped means publishing is
                off or no token is present, which is not a failure.
    truncated — the model stopped mid-document because it ran out of room. The
                files WERE written and carry a banner saying so; publishing was
                skipped. Callers should say so rather than showing a plain
                "Saved:" — a truncated document is the one failure mode that
                looks like success.
    """

    paths: list[Path]
    prepared: PreparedDocument
    session_data: dict
    publish: PublishResult | None = None
    truncated: bool = False


# Prefixed onto a partial document so the incompleteness travels WITH the file
# instead of living only in a dialog someone dismissed. Deliberately plain
# markdown that renderers.py's narrow parser already handles (H2 + paragraphs),
# so it survives into .md, .html and .docx alike.
_TRUNCATION_BANNER = """\
## This document is INCOMPLETE

The AI model ran out of room and stopped part-way through writing this
procedure. The steps below are real, but the end of the process is missing — do
not treat this as a finished SOP. Any screenshots the model never reached are
collected under "Screen reference" at the bottom.

To get a complete document: open the session in AutoDoc, delete screenshots that
don't show a distinct step, and generate again.

"""


def run_generation(
    config: AutoDocConfig,
    session_dir: Path,
    output_dir: Path | None = None,
) -> GenerationResult:
    """
    Generate documentation for a session and write every configured format.

    session.json is re-read from disk here rather than accepted as an argument.
    That is deliberate: the editor used to pass its in-memory dict while the
    sessions list re-read the file, so the two paths could generate from
    different content. Callers that have just edited a session must save it
    before calling this — which is what the editor's Generate button already
    does.

    Args:
        config:      Loaded AutoDocConfig.
        session_dir: Path to the session folder (contains session.json).
        output_dir:  Root output folder. Defaults to the configured one.

    Raises:
        OSError / json.JSONDecodeError: session.json missing or malformed.
        ValueError: session has no usable content.
        generation.backends.base.DocGenerationError: backend failure.
        RuntimeError: every output format failed to write.
    """
    # Imported lazily to keep app startup off the whisper/renderer import cost,
    # matching how the UI layer imported these before.
    from generation.backends.base import TruncatedGenerationError
    from generation.generator import generate_documentation
    from generation.renderers import prepare_document, write_outputs

    if output_dir is None:
        output_dir = get_app_dir() / config.output_dir

    with open(session_dir / "session.json", encoding="utf-8") as f:
        session_data = json.load(f)

    # A truncated generation is kept, not discarded: it cost minutes of GPU time
    # and 18 of 25 steps is a usable starting point. What it must NOT do is look
    # finished or reach the wiki — hence the banner and the publish skip below.
    truncated = False
    try:
        markdown = generate_documentation(
            config=config,
            session_data=session_data,
            session_dir=session_dir,
        )
    except TruncatedGenerationError as exc:
        log.warning("run_generation: %s truncated — keeping partial, skipping publish",
                    session_dir.name)
        truncated = True
        markdown = _TRUNCATION_BANNER + exc.partial_markdown

    prepared = prepare_document(
        markdown=markdown,
        session_data=session_data,
        session_dir=session_dir,
        config=config,
    )
    paths = write_outputs(
        prepared=prepared,
        session_data=session_data,
        config=config,
        output_dir=output_dir,
    )
    log.info(
        "run_generation: %s → %d file(s), %d screenshot(s)",
        session_dir.name,
        len(paths),
        len(prepared.images),
    )
    _cache_markdown(session_dir, prepared.doc)

    # Publish AFTER the local files exist, never instead of them. publish_session
    # never raises for exactly this reason: the files above cost real time and
    # tokens, and a wiki problem must not cost the user that work. Failures are
    # returned, logged, and recorded in session.json for retry.
    #
    # A truncated document is the one thing that does NOT get published. The
    # local files are worth keeping and finishing by hand; a half-written SOP on
    # the wiki is worse than no page, because the next person to read it has no
    # way to know it stops early. publish_existing() can push it later if the
    # user decides the partial is good enough.
    if truncated:
        publish = PublishResult(
            skipped=True,
            reason="the generated document is incomplete (the model ran out of room)",
        )
    else:
        publish = publish_session(
            config=config,
            session_dir=session_dir,
            session_data=session_data,
            prepared=prepared,
        )

    return GenerationResult(
        paths=paths,
        prepared=prepared,
        session_data=session_data,
        publish=publish,
        truncated=truncated,
    )


def publish_existing(
    config: AutoDocConfig,
    session_dir: Path,
) -> PublishResult:
    """
    Retry publishing an already-generated session, with no model call.

    Reads the cached document written by run_generation (see MARKDOWN_CACHE) and
    re-resolves the screenshots from session.json, so a retry publishes the SAME
    document the failed attempt did — and picks up any screenshot edits made
    since. Returns a skipped result if there is no cached document, because in
    that case there is genuinely nothing to publish and Generate is the fix.
    """
    from generation.renderers import prepare_document

    cache = session_dir / MARKDOWN_CACHE
    try:
        markdown = cache.read_text(encoding="utf-8")
    except OSError:
        return PublishResult(
            skipped=True,
            reason="nothing generated for this session yet — run Generate first",
        )

    with open(session_dir / "session.json", encoding="utf-8") as f:
        session_data = json.load(f)

    # Safe to re-run on an already-prepared document: token normalization is
    # idempotent on valid input, and re-running it is what makes a retry reflect
    # screenshots deleted or imported since the original generation.
    prepared = prepare_document(
        markdown=markdown,
        session_data=session_data,
        session_dir=session_dir,
        config=config,
    )
    return publish_session(
        config=config,
        session_dir=session_dir,
        session_data=session_data,
        prepared=prepared,
    )


def _cache_markdown(session_dir: Path, doc: str) -> None:
    """
    Save the prepared document beside the session for retry/republish.

    This is the TOKEN form ({{screenshot:id}}), not the rendered markdown that
    lands in output/ — that one has local relative image paths substituted in and
    is therefore not portable. Writing it here rather than reading output/*.md
    back also keeps retry working when "md" isn't in output_formats at all.

    Derived data: safe to delete, and a write failure is logged, not raised —
    losing the retry cache must not fail a generation that otherwise succeeded.
    """
    try:
        (session_dir / MARKDOWN_CACHE).write_text(doc, encoding="utf-8")
    except OSError as exc:
        log.warning("run: could not cache generated markdown: %s", exc)
