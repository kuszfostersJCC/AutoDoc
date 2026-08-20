"""Documentation-generation backends; get_backend() is the only public entry point."""

from config import AutoDocConfig

from .base import DocGenerationError, GenerationBackend
from .local_vision_backend import LocalVisionBackend

__all__ = [
    "GenerationBackend",
    "DocGenerationError",
    "LocalVisionBackend",
    "get_backend",
]


def get_backend(config: AutoDocConfig) -> GenerationBackend:
    """Build the AI-server backend from config."""
    return LocalVisionBackend(
        endpoint=config.local_endpoint,
        model=config.local_model,
        api_key=config.local_api_key,
    )
