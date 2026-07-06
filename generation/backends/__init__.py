"""
generation/backends/ — Pluggable documentation-generation providers.

AutoDoc supports more than one place to actually run the model that turns a session's
narration + blurred screenshots into markdown documentation:

  "anthropic"  — the Claude API (cloud). Default; requires config.api_key.
  "local"      — any local, OpenAI-compatible vision endpoint (Ollama, LM Studio,
                 llama.cpp server, etc.), so narration and screenshots never leave
                 the machine/network. Requires config.local_endpoint + local_model.

get_backend() is the only thing the rest of the app should call — it reads
config.provider and returns the matching backend instance. Adding a new provider
later means adding one class here and one branch in get_backend(); nothing in the
UI layer or generation/content.py needs to change.
"""

from config import AutoDocConfig

from .anthropic_backend import AnthropicBackend
from .base import DocGenerationError, GenerationBackend
from .local_vision_backend import LocalVisionBackend

__all__ = [
    "GenerationBackend",
    "DocGenerationError",
    "AnthropicBackend",
    "LocalVisionBackend",
    "get_backend",
]


def get_backend(config: AutoDocConfig) -> GenerationBackend:
    """Resolve the configured provider into a concrete backend instance."""
    provider = (config.provider or "anthropic").strip().lower()

    if provider == "anthropic":
        return AnthropicBackend(api_key=config.api_key, model=config.claude_model)

    if provider == "local":
        return LocalVisionBackend(
            endpoint=config.local_endpoint,
            model=config.local_model,
        )

    raise DocGenerationError(
        f"Unknown provider '{provider}' in config.json. Expected 'anthropic' or 'local'."
    )
