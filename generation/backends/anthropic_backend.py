"""
generation/backends/anthropic_backend.py — Claude API backend (cloud).

Translates neutral content blocks into Anthropic's multimodal message format and
calls the Claude API. This is the default provider ("provider": "anthropic" in
config.json) and requires a valid api_key.

Error handling here is intentionally broad: generate() must NEVER let a raw
exception escape (bad key, expired key, network hiccup, rate limit, a bundling
issue in a frozen build, anything) — everything is translated into a
DocGenerationError with a friendly message, because a background-thread crash
here should never look like the whole application crashed to the person who
just clicked "Generate Documentation".

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
                "An AI model is not linked to your application.\n"
                "No Anthropic API key is configured. Contact your administrator, "
                "or set \"provider\": \"local\" in config.json to use a local model instead."
            )

        try:
            blocks = self._to_anthropic_blocks(content_blocks)
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": blocks}],
            )
            return response.content[0].text

        except anthropic.AuthenticationError as exc:
            # Bad/expired/revoked key. This is the case an end user hitting
            # Generate with a placeholder or bad key will see most often.
            raise DocGenerationError(
                "An AI model is not linked to your application.\n"
                "The configured API key was rejected. Contact your administrator."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise DocGenerationError(
                "The Anthropic API is rate-limiting this account right now.\n"
                "Wait a bit and try again, or contact your administrator."
            ) from exc
        except anthropic.APIError as exc:
            raise DocGenerationError(f"Anthropic API error: {exc}") from exc
        except Exception as exc:
            # Catch-all: network failure, SSL/certificate issue in a frozen
            # build, malformed response, anything unanticipated. Always
            # surface as a friendly, catchable error — never let this
            # backend raise something the UI layer doesn't expect.
            raise DocGenerationError(
                f"Could not generate documentation using the Anthropic API.\n"
                f"Detail: {exc}"
            ) from exc

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
