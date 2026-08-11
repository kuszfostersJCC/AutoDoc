"""
generation/backends/local_vision_backend.py — the AI-server backend.

Calls the district's on-premises, OpenAI-compatible chat-completions endpoint.
Narration and screenshots never leave the network, which is the FERPA-relevant
property and the reason this is the ONLY backend AutoDoc has (the Anthropic
one was removed 2026-08-06 — see HANDOFF.md).

--------------------------------------------------------------------------
THE API KEY IS A METERING CREDENTIAL, NOT AN ACCESS CONTROL
--------------------------------------------------------------------------

The server's README is explicit about this and it is the single easiest thing
to get wrong, because getting it wrong LOOKS LIKE IT WORKS:

    "Requests with no key still succeed but get attributed to `chat-ui`,
     mixing your app's traffic into aggregate staff chat and destroying the
     per-app attribution this stack was built for."

So a missing/blank `local_api_key` produces perfectly good documentation while
silently corrupting the year-long per-app token record that exists to justify
buying more GPU. There is no error to notice. That is why:

  * `local_api_key` has a real default in config.py (the `doc-generator` key),
    so the value reaches deployed machines through the normal update path;
  * this backend LOGS A WARNING on every generation when the key is blank,
    rather than treating it as a benign optional setting;
  * the Settings tab reports attribution status instead of hiding the field.

Likewise the endpoint MUST be port 8080 (the metering proxy). Port 8081 is the
llama-server router: it answers correctly and is therefore an attractive
"fix" when the proxy is down, but tokens sent there are invisible to every
metric and dashboard. `endpoint_warnings()` flags it; nothing blocks it,
because it is a legitimate emergency fallback.

--------------------------------------------------------------------------

local_endpoint must include the path, e.g.
http://172.20.28.203:8080/v1/chat/completions — POSTing to the bare host root
is the most common setup mistake.

IMAGE FORMAT — the OpenAI-compatibility mess, handled adaptively:
  * Spec form (llama.cpp server, LM Studio, vLLM, most others):
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}
  * Ollama's documented form: the same but image_url is a BARE STRING.
We send the spec form first; on an HTTP 4xx we retry ONCE with the bare-string
form and log which one worked. The district server is llama.cpp (spec form), so
the retry is dead weight there — it is kept because it costs one branch and
makes a laptop-Ollama fallback work with the same config.

VISION SUPPORT IS NOT GUARANTEED: a GGUF without its vision projector (mmproj)
reports input_modalities ["text"] and will ignore or reject image parts —
generation then quietly degrades to narration-only. list_models() surfaces
modalities so the Settings UI can warn about text-only model choices.

NO PROMPT OR COMPLETION IS EVER LOGGED HERE. The server stack is built so that
no component can persist a request or response body; rule 3 of its README
extends that obligation to client applications. Log metadata (model, endpoint,
sizes, status) and nothing else — an HTTP error body is the one exception, and
it is the server's error text, not the user's content.

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
from urllib.parse import urlsplit

from .base import DocGenerationError, GenerationBackend

log = logging.getLogger(__name__)

# The metering proxy. Anything else on the AI server host is un-metered.
METERED_PORT = 8080
# The llama-server router. Answers requests, bypasses metering entirely.
UNMETERED_ROUTER_PORT = 8081

# Only one model is resident on the card at a time, so a request for a model
# that isn't loaded triggers an unload + load first. The server README puts
# that at "tens of seconds" and tells clients to design timeouts accordingly;
# a long generation with a dozen screenshots on top of a cold start is the
# worst case this has to survive.
DEFAULT_TIMEOUT_S = 600
# The models list is a cheap metadata call — it must never sit for minutes
# behind a Fetch button.
MODELS_TIMEOUT_S = 10


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


def endpoint_warnings(endpoint: str) -> list[str]:
    """
    Non-fatal problems with an endpoint URL, as human-readable strings.

    Split out from generate() so the Settings tab can show the same warnings
    at configuration time rather than leaving the operator to discover a
    metering hole a month later on the Grafana dashboard. Returns [] for a
    healthy endpoint. Never raises — a URL this cannot parse is left to the
    request itself to reject.
    """
    warnings: list[str] = []
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return warnings
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return warnings

    if port == UNMETERED_ROUTER_PORT:
        warnings.append(
            f"Port {UNMETERED_ROUTER_PORT} is the llama-server router, which BYPASSES "
            f"metering — requests will work but their tokens are invisible to every "
            f"dashboard. Use port {METERED_PORT} (the metering proxy) unless the proxy "
            f"is down and this is a deliberate temporary fallback."
        )
    if not parts.path.rstrip("/").endswith("/chat/completions"):
        warnings.append(
            "The endpoint should end in /v1/chat/completions — pointing at the host "
            "root is the most common setup mistake."
        )
    return warnings


def _auth_headers(api_key: str) -> dict:
    """
    Bearer header for the metering proxy, or {} when no key is configured.

    Sending no header is NOT an error condition at the server — the request
    succeeds and is billed to `chat-ui`. Callers log the consequence; this
    helper just builds the header.
    """
    api_key = (api_key or "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(endpoint: str, api_key: str = "", timeout_s: int = MODELS_TIMEOUT_S) -> list[dict]:
    """
    Query the server's /v1/models. Returns a list of
    {"id": str, "text_only": bool} sorted by id. text_only is True only when
    the server EXPLICITLY reports input modalities without "image"
    (llama.cpp does; Ollama doesn't report modalities → assume capable).

    Carries the API key even though the proxy forwards unmetered paths
    untouched: if key enforcement is ever tightened on the proxy, a Fetch that
    silently stopped working would be a confusing way to find out.

    Raises DocGenerationError with a friendly message on any failure —
    callers (Settings tab) show it verbatim.
    """
    url = models_url_from_endpoint(endpoint)
    try:
        request = urllib.request.Request(url, headers=_auth_headers(api_key), method="GET")
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
    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s

    def generate(self, system_prompt: str, content_blocks: list[dict]) -> str:
        if not self.endpoint or not self.endpoint.strip():
            raise DocGenerationError(
                "No AI server endpoint is configured.\n"
                "Set the endpoint in Settings (e.g. "
                "http://172.20.28.203:8080/v1/chat/completions)."
            )

        # Warn, don't fail. Both of these produce working documentation and
        # broken metrics, so the only place they can surface is the log —
        # which is exactly why they must actually be written to it.
        if not (self.api_key or "").strip():
            log.warning(
                "local backend: NO API KEY configured — this request will be "
                "attributed to 'chat-ui' instead of 'doc-generator' on the AI "
                "server, corrupting per-app token accounting. Set local_api_key "
                "in config.json."
            )
        for warning in endpoint_warnings(self.endpoint):
            log.warning("local backend: %s", warning)

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
                    "The AI server rejected the request in BOTH image formats. If "
                    "the selected model is text-only (no vision projector / mmproj), "
                    "it cannot process screenshots — pick a vision-capable model in "
                    "Settings.\n"
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
                # Non-streaming on purpose: there is no partial-output UI to
                # feed, and the proxy reads token counts out of the final
                # response either way. Deliberately NOT setting
                # stream_options.include_usage — that is for streaming clients
                # and the proxy injects it itself (server README, rule 4).
                "stream": False,
            }

            headers = {"Content-Type": "application/json", **_auth_headers(self.api_key)}
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )

            # Metadata only — never the prompt, never the completion.
            log.info(
                "local backend: POST %s model=%s blocks=%d images=%d attributed=%s",
                self.endpoint,
                self.model,
                len(content_blocks),
                sum(1 for b in content_blocks if b["type"] == "image"),
                "doc-generator (keyed)" if (self.api_key or "").strip() else "chat-ui (NO KEY)",
            )

            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # Token counts, for the log line only. llama.cpp attaches `timings`
            # alongside the standard `usage`; either may be absent depending on
            # what sits in front. Never fatal — this is telemetry, not output.
            usage = body.get("usage") or {}
            if usage:
                log.info(
                    "local backend: usage prompt=%s completion=%s",
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )

            return body["choices"][0]["message"]["content"]

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403):
                # The proxy currently lets unkeyed requests through, so this
                # means a key was sent and REJECTED — a stale key after a
                # rotation, not a missing one. Say so precisely; "check your
                # key" sends people to look at a field that is populated.
                raise DocGenerationError(
                    f"The AI server rejected AutoDoc's API key (HTTP {exc.code}).\n"
                    "The key was probably rotated on the server. Ask IT for the "
                    "current 'doc-generator' key and update local_api_key in "
                    "config.json.\n"
                    f"Detail: {detail}"
                ) from exc
            if 400 <= exc.code < 500:
                # Might be the image-format incompatibility — let generate()
                # decide whether a retry in the other form makes sense.
                raise _RetryableFormatError(
                    f"AI server at {self.endpoint} returned HTTP {exc.code}.\n"
                    f"Detail: {detail}",
                    http_code=exc.code,
                ) from exc
            raise DocGenerationError(
                f"AI server at {self.endpoint} returned HTTP {exc.code}.\n"
                f"Detail: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise DocGenerationError(
                f"Could not reach the AI server at {self.endpoint}.\n"
                f"Is it running, and are you on the district network? Detail: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            # Distinguish this from "server down": only one model fits on the
            # card, so asking for a model that isn't resident costs an unload
            # and a load before any work starts.
            raise DocGenerationError(
                f"The AI server at {self.endpoint} did not respond within "
                f"{self.timeout_s}s.\n"
                "If another application is using a different model, the server has "
                "to swap models before it can answer, which can take a while. Try "
                "again in a minute."
            ) from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise DocGenerationError(
                f"Unexpected response from the AI server at {self.endpoint}: {exc}"
            ) from exc
        except (_RetryableFormatError, DocGenerationError):
            raise
        except Exception as exc:
            # Catch-all: anything unanticipated still becomes a friendly,
            # catchable error instead of an uncaught background-thread crash.
            raise DocGenerationError(
                f"Could not generate documentation using the AI server.\n"
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
    """HTTP 4xx from the AI server — possibly the image-format quirk."""

    def __init__(self, message: str, http_code: int) -> None:
        super().__init__(message)
        self.http_code = http_code
