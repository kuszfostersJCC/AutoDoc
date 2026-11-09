"""Backend-agnostic entry point for documentation generation; the UI calls only this."""

from pathlib import Path

from config import AutoDocConfig
from generation.backends import get_backend
from generation.content import build_content_blocks, get_system_prompt, resolve_session_mode


def generate_documentation(
    config: AutoDocConfig,
    session_data: dict,
    session_dir: Path,
) -> str:
    """Generate markdown documentation for a session using the configured AI server.

    Raises ValueError on an empty session, DocGenerationError on backend failures.
    """
    content_blocks = build_content_blocks(session_data, session_dir, config)
    # build_content_blocks() always emits a preamble block, so one block means empty.
    if len(content_blocks) <= 1:
        raise ValueError(
            "No usable content found in this session.\n"
            "The session may have no transcript and no screenshots."
        )

    mode = resolve_session_mode(session_data, config)
    backend = get_backend(config)
    return backend.generate(get_system_prompt(mode), content_blocks)
