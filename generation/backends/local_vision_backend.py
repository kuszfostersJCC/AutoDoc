"""
generation/backends/local_vision_backend.py — Local vision-model backend.

Calls a local, OpenAI-compatible chat-completions endpoint instead of a cloud API, so
narration and blurred screenshots never leave the machine or local network. This is
the option for FERPA-sensitive workflows where even blurred images shouldn't be sent
to a third party at all.

local_endpoint must include the path, e.g. http://localhost:11434/v1/chat/completions
— POSTing to the bare host root returns a 405 (that endpoint only serves a GET status
check), which is the most common setup mistake.

Image format note: Ollama's own /v1/chat/completions docs (docs.ollama.com/api/
openai-compatibility, verified against a live fetch) show `image_url` as a bare
base64 data-URI *string*, not the `{"url": "..."}` object the official OpenAI spec
uses elsewhere. We send the bare-string form here since that's what Ollama's
documented example actually parses. If you point this at a different OpenAI-
compatible server (LM Studio, llama.cpp's server, etc.) and get an error on the
image field, try switching this to `{"url": f"data:image/png;base64,{b64}"}` instead
— that's the more spec-correct alternative most other servers expect.

No new third-party dependency is introduced for this — it's a single JSON POST over
stdlib urllib, since most local inference servers speak plain HTTP.
"""

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from .base import DocGenerationError, GenerationBackend


class LocalVisionBackend(GenerationBackend):
    def __init__(self, endpoint: str, model: str, timeout_s: int = 300) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout_s = timeout_s

    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        if not self.endpoint or not self.endpoint.strip():
            raise DocGenerationError(
                "No local model endpoint is configured.\n"
                "Set 'local_endpoint' in config.json (e.g. "
                "http://localhost:11434/v1/chat/completions)."
            )

        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": self._to_openai_content(content_blocks)},
                ],
                "stream": False,
            }

            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            return body["choices"][0]["message"]["content"]

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DocGenerationError(
                f"Local model server at {self.endpoint} returned HTTP {exc.code}.\n"
                f"Detail: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise DocGenerationError(
                f"Could not reach local model server at {self.endpoint}.\n"
                f"Is it running? Detail: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise DocGenerationError(
                f"Local model server at {self.endpoint} timed out after {self.timeout_s}s."
            ) from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise DocGenerationError(
                f"Unexpected response from local model server at {self.endpoint}: {exc}"
            ) from exc
        except Exception as exc:
            # Catch-all: anything unanticipated still becomes a friendly,
            # catchable error instead of an uncaught background-thread crash.
            raise DocGenerationError(
                f"Could not generate documentation using the local model.\n"
                f"Detail: {exc}"
            ) from exc

    @staticmethod
    def _to_openai_content(content_blocks: list[dict]) -> list[dict]:
        """
        Convert neutral blocks into OpenAI-style {type, image_url} content parts.

        image_url is sent as a bare base64 data-URI string to match Ollama's
        documented /v1/chat/completions example (see module docstring) — not the
        {"url": "..."} object form some other OpenAI-compatible servers expect.
        """
        parts: list[dict] = []
        for block in content_blocks:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                path: Path = block["path"]
                b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": f"data:image/png;base64,{b64}",
                })
        return parts
