"""Abstract interface every generation backend implements."""

from abc import ABC, abstractmethod


class DocGenerationError(RuntimeError):
    """Raised when a backend cannot produce documentation (auth, network, model, etc.)."""


class TruncatedGenerationError(DocGenerationError):
    """Model hit the length limit; carries the partial markdown so callers can keep it."""

    def __init__(self, message: str, partial_markdown: str) -> None:
        super().__init__(message)
        self.partial_markdown = partial_markdown


class GenerationBackend(ABC):
    """A pluggable backend that turns session content into markdown documentation."""

    @abstractmethod
    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        """Blocks are {"type": "text", "text": str} or {"type": "image", "path": Path}.

        Raises DocGenerationError on any failure.
        """
        raise NotImplementedError
