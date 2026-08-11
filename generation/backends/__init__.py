"""
generation/backends/ — the documentation-generation backend.

ONE BACKEND, ONE SERVER (2026-08-06). AutoDoc talks to the district's
on-premises AI server (`local_endpoint`, `local_model`, `local_api_key`) and
nothing else.

The Anthropic/Claude-API backend that used to live here was DELETED, not
disabled. It had been dead code since 2026-07-29 when `provider` defaulted to
"local", and keeping it cost more than it was worth:

  * it made "which backend am I on?" a live question in a FERPA workload where
    the answer must be "the one inside the building" — the strongest version of
    that guarantee is not having a cloud code path at all;
  * it dragged `anthropic` → `httpx` → `anyio` → `charset_normalizer` into the
    frozen build for a feature nobody would ever turn on;
  * `provider`, `api_key` and `claude_model` were three config keys, a Settings
    dropdown and a first-run prompt that existed only to select between one
    real option and one nobody wanted.

The `GenerationBackend` ABC in base.py survives that removal on purpose: it is
what keeps generation/content.py and generation/generator.py free of wire-format
details, and it is a one-file, one-branch cost to add a second backend if the
question ever genuinely reopens. Restoring a CLOUD backend, though, is a policy
decision and not just a code change — see the FERPA notes in HANDOFF.md.

get_backend() is the only thing the rest of the app should call.
"""

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
    """
    Build the AI-server backend from config.

    Kept as a function rather than inlined at the call site so that the
    api_key/endpoint/model wiring lives in exactly one place — the metering
    key in particular must not be something a future caller can forget to
    pass, because omitting it produces working output and broken metrics.
    """
    return LocalVisionBackend(
        endpoint=config.local_endpoint,
        model=config.local_model,
        api_key=config.local_api_key,
    )
