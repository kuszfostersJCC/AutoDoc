"""
generation/backends/local_vision_backend.py — Local vision-model backend.

Calls a local, OpenAI-compatible chat-completions endpoint instead of a cloud
API, so narration and screenshots never leave the machine or local network.
This is the option for FERPA-sensitive workflows where even redacted images
shouldn't be sent to a third party at all.

local_endpoint must include the path, e.g.
http://172.20.28.203:8080/v1/chat/completions — POSTing to the bare host root
is the most common setup mistake.

IMAGE FORMAT — the OpenAI-compatibility mess, handled adaptively:
  * Spec form (llama.cpp server, LM Studio, vLLM, most others):
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}
  * Ollama's documented form: the same but image_url is a BARE STRING.
We send the spec form first; on an HTTP 4xx we retry ONCE with the bare-string
form and log which one worked. This keeps one config working across Ollama on
a laptop and llama.cpp on the AI server without a user-visible switch.

VISION SUPPORT IS NOT GUARANTEED: a GGUF without its vision projector (mmproj)
reports input_modalities ["text"] and will ignore or reject image parts —
generation then quietly degrades to narration-only. list_models() surfaces
modalities so the Settings UI can warn about text-only model choices.

No new third-party dependency — plain JSON over stdlib urllib, since every
local inference server speaks plain HTTP.
"""

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

from .base import DocGenerationError, GenerationBackend

log = logging.getLogger(__name__)


def models_url_from_endpoint(endpoint: str) -> str:
    """
    Derive the /v1/models URL from a chat-completions endpoint.
    "…/v1/chat/completions" → "…/v1/models"; anything else gets
    "/v1/models" appended to the host root as a best guess.
    """
    endpoint = (endpoint or "").strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint[: -len("/chat/completions")] + "/models"
    match = re.match(r"^(https?://[^/]+)", endpoint)
    if match:
        return match.group(1) + "/v1/models"
    return endpoint + "/v1/models"


def list_models(endpoint: str, timeout_s: int = 10) -> list[dict]:
    """
    Query the server's /v1/models. Returns a list of
    {"id": str, "text_only": bool} sorted by id. text_only is True only when
    the server EXPLICITLY reports input modalities without "image"
    (llama.cpp does; Ollama doesn't report modalities → assume capable).

    Raises DocGenerationError with a friendly message on any failure —
    callers (Settings tab) show it verbatim.
    """
    url = models_url_from_endpoint(endpoint)
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = []
        for entry in body.get("data", []):
            model_id = entry.get("id")
            if not model_id:
                continue
            modalities = (entry.get("architecture") or {}).get("input_modalities")
            text_only = bool(modalities) and "image" not in modalities
            models.append({"id": model_id, "text_only": text_only})
        return sorted(models, key=lambda m: m["id"])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise DocGenerationError(
            f"Could not list models from {url}: {exc}"
        ) from exc


class LocalVisionBackend(GenerationBackend):
    def __init__(self, endpoint: str, model: str, timeout_s: int = 300) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout_s = timeout_s

    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        if not self.endpoint or not self.endpoint.strip():
            raise DocGenerationError(
                "No local model endpoint is configured.\n"
                "Set the local endpoint in Settings (e.g. "
                "http://172.20.28.203:8080/v1/chat/completions)."
            )

        has_images = any(b["type"] == "image" for b in content_blocks)

        # Spec-form first (llama.cpp/LM Studio/vLLM), bare-string fallback
        # (Ollama). Only retry when images are present — text-only payloads
        # are identical in both forms, so a 4xx there is a real error.
        try:
            return self._post(system_prompt, content_blocks, bare_image_strings=False)
        except _RetryableFormatError as first_error:
            if not has_images:
                raise DocGenerationError(str(first_error)) from first_error
            log.info(
                "local backend: spec-form image payload rejected (HTTP %s) — "
                "retrying with Ollama bare-string form", first_error.http_code,
            )
            try:
                result = self._post(system_prompt, content_blocks, bare_image_strings=True)
                log.info("local backend: bare-string image form accepted")
                return result
            except _RetryableFormatError as second_error:
                raise DocGenerationError(
                    "The local model server rejected the request in BOTH image "
                    "formats. If the selected model is text-only (no vision "
                    "projector / mmproj), it cannot process screenshots — pick "
                    "a vision-capable model in Settings.\n"
                    f"First attempt: {first_error}\nSecond attempt: {second_error}"
                ) from second_error

    def _post(self, system_prompt: str, content_blocks: list[dict], bare_image_strings: bool) -> str:
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": self._to_openai_content(content_blocks, bare_image_strings),
                    },
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
            if 400 <= exc.code < 500:
                # Might be the image-format incompatibility — let generate()
                # decide whether a retry in the other form makes sense.
                raise _RetryableFormatError(
                    f"Local model server at {self.endpoint} returned HTTP {exc.code}.\n"
                    f"Detail: {detail}",
                    http_code=exc.code,
                ) from exc
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
        except (_RetryableFormatError, DocGenerationError):
            raise
        except Exception as exc:
            # Catch-all: anything unanticipated still becomes a friendly,
            # catchable error instead of an uncaught background-thread crash.
            raise DocGenerationError(
                f"Could not generate documentation using the local model.\n"
                f"Detail: {exc}"
            ) from exc

    @staticmethod
    def _to_openai_content(content_blocks: list[dict], bare_image_strings: bool) -> list[dict]:
        """Convert neutral blocks into OpenAI-style content parts, in either
        the spec image form or Ollama's bare-string form."""
        parts: list[dict] = []
        for block in content_blocks:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                path: Path = block["path"]
                b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
                data_uri = f"data:image/png;base64,{b64}"
                parts.append({
                    "type": "image_url",
                    "image_url": data_uri if bare_image_strings else {"url": data_uri},
                })
        return parts


class _RetryableFormatError(Exception):
    """HTTP 4xx from the local server — possibly the image-format quirk."""

    def __init__(self, message: str, http_code: int) -> None:
        super().__init__(message)
        self.http_code = http_code
