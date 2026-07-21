"""
generation/generator.py — Single, provider-agnostic entry point for documentation
generation. This is the only function the UI layer (sessions_window, session_editor)
should call.

Swapping between the Claude API and a local vision model is a config.json change
("provider": "anthropic" | "local") — nothing here, in generation/content.py, or in
the UI needs to change when that happens.
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
    Generate markdown documentation for a session using whichever backend is
    configured (config.provider).

    Args:
        config:       Loaded AutoDocConfig (holds provider selection + credentials).
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
