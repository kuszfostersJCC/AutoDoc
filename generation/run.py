"""The one generate-then-write-then-publish sequence; new pipeline steps go here.

Blocking, so call from a worker thread only; progress reporting is the caller's,
via ui_service.call_soon().
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import AutoDocConfig, get_app_dir
from generation.renderers import PreparedDocument
from publish.bookstack_publisher import PublishResult, publish_session

log = logging.getLogger(__name__)

# Token-form markdown (not rendered output), cached so a publish can be retried.
MARKDOWN_CACHE = "last_generated.md"


@dataclass
class GenerationResult:
    """Result of one run; paths[0] is the primary file, publish is None if it never ran.

    A publish result with `.skipped` set means publishing was off, not that it failed.
    `truncated` means the files were written with an incomplete-document banner.
    """

    paths: list[Path]
    prepared: PreparedDocument
    session_data: dict
    publish: PublishResult | None = None
    truncated: bool = False


# Restricted to H2 + paragraphs: that is all renderers.py's parser handles.
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
    """Generate documentation for a session and write every configured format.

    session.json is re-read from disk here, so callers must save any edits first.
    """
    # Imported lazily to keep the whisper/renderer import cost out of app startup.
    from generation.backends.base import TruncatedGenerationError
    from generation.generator import generate_documentation
    from generation.renderers import prepare_document, write_outputs

    if output_dir is None:
        output_dir = get_app_dir() / config.output_dir

    with open(session_dir / "session.json", encoding="utf-8") as f:
        session_data = json.load(f)

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

    # publish_session never raises; failures come back as a PublishResult for retry.
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
    """Retry publishing from MARKDOWN_CACHE, with no model call.

    Screenshots are re-resolved, so edits made since the original run are picked up.
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

    # Safe on an already-prepared document: token normalization is idempotent.
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
    """Cache the token form ({{screenshot:id}}), not the rendered output, for republish.

    Derived data: a write failure is logged, not raised.
    """
    try:
        (session_dir / MARKDOWN_CACHE).write_text(doc, encoding="utf-8")
    except OSError as exc:
        log.warning("run: could not cache generated markdown: %s", exc)
