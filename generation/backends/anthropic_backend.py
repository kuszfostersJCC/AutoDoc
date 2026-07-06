"""
generation/backends/anthropic_backend.py — Claude API backend (cloud).

Translates neutral content blocks into Anthropic's multimodal message format and
calls the Claude API. This is the default provider ("provider": "anthropic" in
config.json) and requires a valid api_key.

FERPA note: using this backend means blurred screenshots and the text transcript
are sent to Anthropic's API. Raw audio never leaves the machine regardless of which
backend is selected — that boundary is enforced upstream in transcription/, not here.
For workflows where even blurred images must never leave the building, switch
"provider" to "local" instead (see local_vision_backend.py).
"""

import base64
from pathlib import Path

import anthropic

from .base import DocGenerationError, GenerationBackend


class AnthropicBackend(GenerationBackend):
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        if not self.api_key or not self.api_key.strip():
            raise DocGenerationError(
                "No Anthropic API key is configured.\n"
                "Set 'api_key' in config.json, or set \"provider\": \"local\" "
                "to use a local model instead."
            )

        blocks = self._to_anthropic_blocks(content_blocks)

        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": blocks}],
            )
        except anthropic.APIError as exc:
            raise DocGenerationError(f"Anthropic API error: {exc}") from exc

        return response.content[0].text

    @staticmethod
    def _to_anthropic_blocks(content_blocks: list[dict]) -> list[dict]:
        """Convert neutral blocks into Anthropic's {type, source} image format."""
        blocks: list[dict] = []
        for block in content_blocks:
            if block["type"] == "text":
                blocks.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                path: Path = block["path"]
                b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                })
        return blocks
