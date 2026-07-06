"""
generation/backends/base.py — Abstract interface every generation backend implements.

A backend's only job is: given a system prompt and a list of neutral content blocks
(see generation/content.py), call whatever model/API it wraps and return the generated
markdown as a string. Everything provider-specific (wire format, auth, image encoding,
error handling) is the backend's problem — callers never see it.
"""

from abc import ABC, abstractmethod


class DocGenerationError(RuntimeError):
    """Raised when a backend cannot produce documentation (auth, network, model, etc.)."""


class GenerationBackend(ABC):
    """A pluggable backend that turns session content into markdown documentation."""

    @abstractmethod
    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        """
        Args:
            system_prompt:  Instructions for the assistant (structure, tone, rules).
            content_blocks: Neutral blocks, each one of:
                {"type": "text", "text": str}
                {"type": "image", "path": Path}   # PNG file already on disk

        Returns:
            The generated markdown document as a string.

        Raises:
            DocGenerationError: on any failure — bad/missing credentials, unreachable
                server, malformed response, etc. Callers only need to catch this one
                type regardless of which backend is active.
        """
        raise NotImplementedError
