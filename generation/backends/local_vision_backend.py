"""Backend for the on-premises, OpenAI-compatible chat-completions endpoint.

Never logs prompt or completion text — metadata only.
"""

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .base import DocGenerationError, GenerationBackend, TruncatedGenerationError

log = logging.getLogger(__name__)

# Port 8080 is the metering proxy; 8081 answers requests but bypasses metering.
METERED_PORT = 8080
UNMETERED_ROUTER_PORT = 8081

# Long because only one model is resident: a non-resident model costs an
# unload + load before any work starts.
DEFAULT_TIMEOUT_S = 600
MODELS_TIMEOUT_S = 10

# Substrings marking a 4xx body as "did not fit" rather than "was malformed".
# Biased to misses: a miss costs one retry, a false hit disables the image retry.
_CONTEXT_ERROR_MARKERS = (
    "context size",
    "context length",
    "context window",
    "context_length_exceeded",
    "exceeds the available context",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "n_ctx",
    "kv cache is full",
)


def _looks_like_context_overflow(detail: str) -> bool:
    """True if a server error body reads like 'this did not fit', not 'this was malformed'."""
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _CONTEXT_ERROR_MARKERS)


def models_url_from_endpoint(endpoint: str) -> str:
    """Derive the /v1/models URL from a chat-completions endpoint, or guess from the host."""
    endpoint = (endpoint or "").strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint[: -len("/chat/completions")] + "/models"
    match = re.match(r"^(https?://[^/]+)", endpoint)
    if match:
        return match.group(1) + "/v1/models"
    return endpoint + "/v1/models"


def endpoint_warnings(endpoint: str) -> list[str]:
    """Non-fatal problems with an endpoint URL, for the log and the Settings tab.

    Never raises; an unparseable URL is left for the request itself to reject.
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
    """Bearer header for the metering proxy, or {} when no key is configured.

    An unkeyed request still succeeds server-side, attributed to `chat-ui`.
    """
    api_key = (api_key or "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(endpoint: str, api_key: str = "", timeout_s: int = MODELS_TIMEOUT_S) -> list[dict]:
    """Query /v1/models; returns [{"id": str, "text_only": bool}] sorted by id.

    text_only only when the server explicitly reports modalities without "image";
    a server that reports none is assumed vision-capable. Raises DocGenerationError
    with a message callers can show verbatim.
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

        # Warn, don't fail: both produce working documentation and broken metrics.
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

        # Spec-form image parts first (llama.cpp/LM Studio/vLLM), Ollama's
        # bare-string form on retry. Text-only payloads are identical in both.
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
                    "The AI server rejected the request in BOTH image formats.\n"
                    "Most likely the selected model is text-only (no vision projector "
                    "/ mmproj) and cannot process screenshots at all — pick a "
                    "vision-capable model in Settings.\n"
                    "If the model definitely does support images, the server's own "
                    "error text below is the thing to read.\n"
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
                # No partial-output UI to feed, and the proxy counts tokens either way.
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

            # Telemetry only; `usage` may be absent depending on what serves the model.
            usage = body.get("usage") or {}
            if usage:
                log.info(
                    "local backend: usage prompt=%s completion=%s",
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )

            choice = body["choices"][0]
            content = choice["message"]["content"]

            # "length" means the model ran out of room, not that the document is
            # finished; nothing downstream can tell a truncated document from a whole one.
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                log.warning(
                    "local backend: generation TRUNCATED (finish_reason=length) "
                    "after %s completion tokens",
                    usage.get("completion_tokens", "?"),
                )
                raise TruncatedGenerationError(
                    "The model ran out of room and stopped part-way through the "
                    "document.\n"
                    "AutoDoc kept what it produced and marked it incomplete, but it "
                    "did NOT publish it to the wiki.\n"
                    "What helps, in order of effort:\n"
                    "  • Open the session in the editor and delete screenshots that "
                    "don't show a distinct step — images cost far more of the "
                    "model's room than the narration does.\n"
                    "  • Split a long procedure into two recordings.\n"
                    f"  • Ask IT whether '{self.model}' can be served with a larger "
                    "context window.",
                    partial_markdown=content,
                )
            return content

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise self._classify_http_error(exc.code, detail, content_blocks) from exc
        except urllib.error.URLError as exc:
            raise DocGenerationError(
                f"Could not reach the AI server at {self.endpoint}.\n"
                f"Is it running, and are you on the district network? Detail: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
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
            # Broad on purpose: an uncaught error here would crash a worker thread.
            raise DocGenerationError(
                f"Could not generate documentation using the AI server.\n"
                f"Detail: {exc}"
            ) from exc

    def _classify_http_error(self, code: int, detail: str, content_blocks: list[dict]) -> Exception:
        """Returns (not raises) the exception an HTTP error deserves, so the call
        site keeps its `raise ... from exc` chaining.

        Only return _RetryableFormatError for a 4xx we cannot identify: the retry
        re-uploads every screenshot in the session.
        """
        if code in (401, 403):
            # Unkeyed requests are allowed through, so this means a key was rejected.
            return DocGenerationError(
                f"The AI server rejected AutoDoc's API key (HTTP {code}).\n"
                "The key was probably rotated on the server. Ask IT for the "
                "current 'doc-generator' key and update local_api_key in "
                "config.json.\n"
                f"Detail: {detail}"
            )

        image_count = sum(1 for b in content_blocks if b["type"] == "image")

        if code == 413:
            # A transport limit (proxy body size), not the model's context limit.
            return DocGenerationError(
                "The AI server refused the request as too large to accept at all "
                f"(HTTP 413), before the model saw it.\n"
                f"AutoDoc sent {image_count} screenshot(s) as one upload.\n"
                "Removing screenshots in the session editor will get you under the "
                "limit; the durable fix is for IT to raise the upload body limit on "
                "the proxy in front of the AI server.\n"
                f"Server detail: {detail}"
            )

        if 400 <= code < 500 and _looks_like_context_overflow(detail):
            return DocGenerationError(
                "This session is too large for the model's context window "
                f"(HTTP {code}).\n"
                f"It was sent with {image_count} screenshot(s), and each screenshot "
                "costs far more of the window than the narration does.\n"
                "What helps, in order of effort:\n"
                "  • Open the session in the editor and delete screenshots that "
                "don't show a distinct step.\n"
                "  • Split a long procedure into two recordings.\n"
                f"  • Ask IT whether '{self.model}' can be served with a larger "
                "context window — and whether the server is dividing its context "
                "across parallel slots, which cuts what any one request can use.\n"
                f"Server detail: {detail}"
            )

        if 400 <= code < 500:
            # Might be the image-format dialect; generate() decides whether to retry.
            return _RetryableFormatError(
                f"AI server at {self.endpoint} returned HTTP {code}.\n"
                f"Detail: {detail}",
                http_code=code,
            )

        return DocGenerationError(
            f"AI server at {self.endpoint} returned HTTP {code}.\n"
            f"Detail: {detail}"
        )

    @staticmethod
    def _to_openai_content(content_blocks: list[dict], bare_image_strings: bool) -> list[dict]:
        """Convert neutral blocks into OpenAI-style content parts."""
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
