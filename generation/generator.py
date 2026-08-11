"""
generation/generator.py — Single, backend-agnostic entry point for documentation
generation. This is the only function the UI layer (sessions_window, session_editor)
should call.

Backend-agnostic still means something after the 2026-08-06 removal of the
Anthropic path, even though there is only one backend left: nothing here or in
generation/content.py knows a wire format, an endpoint or a credential. That
separation is what kept the removal a two-file change.
"""

from pathlib import Path

from config import AutoDocConfig
from generation.backends import get_backend
from generation.content import build_content_blocks, get_system_prompt, resolve_session_mode


def generate_documentation(
    config: AutoDocConfig,
    session_data: dict,
    session_dir: Path,
) -> str:
    """
    Generate markdown documentation for a session using the configured AI server.

    Args:
        config:       Loaded AutoDocConfig (holds the endpoint, model and key).
        session_data: Full parsed session.json dict (may contain edited_full_transcript).
        session_dir:  Path to the session folder (for resolving image filenames).

    Raises:
        ValueError: If there are no usable events (empty session).
        generation.backends.base.DocGenerationError: On backend-level failures
            (bad credentials, unreachable server, malformed response, etc.).
    """
    content_blocks = build_content_blocks(session_data, session_dir, config)
    if not content_blocks:
        raise ValueError(
            "No usable content found in this session.\n"
            "The session may have no transcript and no screenshots."
        )

    mode = resolve_session_mode(session_data, config)
    backend = get_backend(config)
    return backend.generate(get_system_prompt(mode), content_blocks)
