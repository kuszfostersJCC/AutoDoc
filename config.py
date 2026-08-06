"""
config.py — Loads AutoDoc configuration from config.json adjacent to the executable.

--------------------------------------------------------------------------
`AutoDocConfig` IS THE SINGLE SOURCE OF TRUTH (restructured 2026-07-29)
--------------------------------------------------------------------------

Adding a config key used to be a FIVE-place edit — the dataclass, a `_DEFAULTS`
dict, `write_config()`'s hand-written dict, `config.example.json`, and a second
bootstrap dict in `main.py`. Three of those were hand-maintained lists of the
same facts, and two had already drifted before anyone wrote the list down
(`main.py` disagreed with `_DEFAULTS` on `screenshot_mode` and `local_model`,
and it won on a fresh install).

Now there are TWO places, and only one of them is load-bearing:

  1. **The dataclass below.** Field name, type, and default. Everything else is
     derived from it — `default_config()` reads `dataclasses.fields()`,
     `write_config()` uses `asdict()`, and `main.py`'s first-run bootstrap
     dumps `default_config()`.
  2. `config.example.json` — documentation for a human setting up a new
     install. Not read by any code, so it can only go stale, never break
     anything. `scripts/` has no generator for it; keep it in step by hand.

Invariant #9's corollary (a key present in config.json but missing from the
write path is silently deleted on the next Settings save) is now structurally
impossible rather than merely documented: there is no second list to omit it
from.

--------------------------------------------------------------------------
THE DEFAULTS BELOW ARE THE JCC DEPLOYMENT BASELINE, NOT GENERIC FALLBACKS
--------------------------------------------------------------------------

This matters more than it looks, because these values are the ONLY way a new
setting reaches an existing install:

  * `scripts/make_release.py` deliberately strips `config.json` from the
    release zip, and `updater.py`'s apply step is a non-mirroring robocopy — so
    an update can NEVER write a deployed `config.json`. That is by design (it's
    what stops an update eating someone's api_key or endpoint).
  * But this file is CODE. It lands in `_internal/`, which is exactly what the
    update robocopy overwrites. And `load_config()` fills any key absent from
    config.json from the defaults here.

So: a new key added here reaches every machine on the next "Check for Updates",
carrying the value written here. That is the whole propagation mechanism, and
it's why these defaults describe the real deployment (provider `local`, the
district's AI server) rather than upstream-generic values.

**Its one hard limit:** defaults only fill keys that are ABSENT. Changing a
value here does NOT change a machine whose config.json already has that key —
which is correct for per-machine settings (api_key), and is also why moving the
AI server off 172.20.28.203 still means visiting each machine or walking someone
through the Settings tab. Deliberately not solved; see HANDOFF.md.
"""

import json
import sys
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path


def get_app_dir() -> Path:
    """Returns the directory containing the executable (when frozen) or the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


@dataclass
class AutoDocConfig:
    # Empty default rather than a required field, so that every field is
    # self-describing and default_config() needs no special case. Nothing
    # constructs this without an api_key anyway (load_config always supplies
    # one), and "" is what a local-provider install legitimately has.
    api_key: str = ""
    whisper_model: str = "base"
    focus_radius_px: int = 200
    blur_strength: int = 15
    # How screenshots are RENDERED for viewing/export/generation:
    #   "raw"     — untouched screen capture
    #   "blurred" — privacy blur outside a clear circle at the click (legacy default)
    #   "circled" — untouched capture with a light red circle marking the click
    # Since 2026-07-08 screenshots are STORED raw and this only controls
    # rendering, so it can be changed per-session in the editor after the fact.
    screenshot_mode: str = "raw"
    circle_radius_px: int = 36
    # Formats written on Generate Documentation (PRD F2). Any subset of
    # "md", "html", "docx"; first entry is what the "Saved:" dialog opens.
    output_formats: list = field(default_factory=lambda: ["docx", "html", "md"])
    # Global hotkeys, active only while recording. pynput GlobalHotKeys
    # syntax (e.g. "<ctrl>+<shift>+s"); empty string disables a hotkey.
    # Editable from the Settings tab in the Sessions window.
    manual_capture_hotkey: str = "<ctrl>+<shift>+s"   # click-less screenshot (PRD F4)
    # NOT ctrl+shift+p — that opens the system print dialog in browsers and
    # Office, which is exactly where people record workflows.
    pause_hotkey: str = "<ctrl>+<alt>+p"              # toggle pause/resume
    # Stop WITHOUT clicking the tray icon — tray clicks would be captured as
    # screenshots that only exist to end the recording.
    stop_hotkey: str = "<ctrl>+<shift>+e"
    sessions_dir: str = "sessions"
    output_dir: str = "output"
    claude_model: str = "claude-sonnet-5"
    max_screenshots_per_session: int = 100
    max_image_dimension: int = 1280
    # Which backend generates documentation: "anthropic" (cloud) or "local"
    # (any OpenAI-compatible local vision server — Ollama, LM Studio, etc.).
    # See generation/backends/ for the implementations.
    #
    # DEFAULTS TO THE REAL DEPLOYMENT (changed 2026-07-29 from "anthropic" +
    # localhost:11434). Two reasons, and the second is the one that mattered:
    #   * FERPA — narration and screenshots never leaving the district network
    #     is the intended posture, so it should also be the default posture.
    #   * A fresh/reimaged machine used to bootstrap to provider "anthropic"
    #     with no key, i.e. a config that cannot generate documentation at all
    #     until someone edits it. HANDOFF.md listed that as a standing reason
    #     new machines still needed the USB stick. They now bootstrap to a
    #     working config; the USB stick is only needed for models/base.pt and
    #     the BookStack token.
    # Side effect worth knowing: main.py's first-run API-key prompt only fires
    # when provider == "anthropic", so a source checkout no longer nags for a
    # key it wouldn't use.
    provider: str = "local"
    local_endpoint: str = "http://172.20.28.203:8080/v1/chat/completions"
    local_model: str = "gemma4-e4b-fast"
    # BookStack publishing (v1.3 — see PRD-v1.3-bookstack.md). Off by default.
    # The bot TOKEN is deliberately not here: this dataclass round-trips through
    # write_config() below, which rewrites config.json in full, so a credential
    # living here would be one Settings-tab save away from being wiped on any
    # machine. It lives in its own file — see publish/token.py.
    bookstack_enabled: bool = False
    bookstack_base_url: str = "http://jcbookstack.jccmi.edu"
    # Single holding book for now; how pages get organised is a later decision.
    bookstack_book: str = "AutoDoc"
    bookstack_timeout_s: int = 30
    # Tag defaults. PROVISIONAL — validated against publish/tags.py's vocabulary,
    # and an empty string means "don't apply this tag".
    bookstack_tag_doc_type: str = "sop"
    bookstack_tag_owner: str = "it-ops"
    bookstack_tag_audience: str = "all-staff"
    bookstack_tag_review_cycle: str = "ad-hoc"
    # UI appearance: "light" | "dark". Validated by theme/tokens.py's
    # resolve_theme(), which falls back to light with a log line rather than
    # raising — a typo here must not stop someone recording. Read early in
    # main() (before any window exists) and again by the Settings tab, which
    # applies a change live.
    #
    # Ships LIGHT on purpose, even though dark mode is the new feature: not
    # everyone wants a dark UI, anyone who does will find the Settings toggle,
    # and dark has had no visual verification yet. Worth revisiting as a default
    # once someone has actually looked at both themes on a real display.
    theme: str = "light"


def default_config() -> dict:
    """
    A FRESH dict of every config key at its default value, derived from
    AutoDocConfig's fields.

    Returns a new dict — and, for `output_formats`, a new list — on every call.
    That is not incidental tidiness: a single module-level defaults dict would
    hand the SAME list object to every AutoDocConfig built from it, so anything
    that mutated `cfg.output_formats` in place would silently change the
    defaults for the rest of the process. (Today the Settings tab rebinds rather
    than mutates, so nothing hits it — which is exactly the kind of latent trap
    worth closing while it's still theoretical.)

    Used for three things: filling keys absent from config.json, filtering
    unknown keys out of it, and writing the first-run config.json in main.py.
    """
    out = {}
    for f in fields(AutoDocConfig):
        if f.default_factory is not MISSING:      # checked FIRST: a field with
            out[f.name] = f.default_factory()      # a factory has default=MISSING
        else:
            out[f.name] = f.default
    return out


def load_config() -> AutoDocConfig:
    config_path = get_app_dir() / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.json not found at {config_path}.\n"
            "Please place config.json next to the AutoDoc executable."
        )
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    # Defaults fill keys the file is missing (this is how a new setting reaches
    # an install that updated rather than being reimaged — see the module
    # docstring), and also act as the whitelist: an unrecognised key in
    # config.json is dropped here rather than blowing up AutoDocConfig(**merged).
    # That's what lets config.example.json carry "_comment" keys.
    defaults = default_config()
    merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
    return AutoDocConfig(**merged)


def write_config(cfg: AutoDocConfig) -> None:
    """
    Rewrite config.json in FULL from the in-memory config.

    HANDOFF.md invariant #9: this writes the whole file from memory, so
    hand-editing a deployed config.json while AutoDoc is open and then pressing
    Save in Settings silently discards the hand-edit. Don't "fix" that by
    merging with the file on disk — merging would leave the in-memory config and
    the file disagreeing in a way nothing else in the app expects.

    The invariant's old corollary — that a key missing from this function's dict
    is silently DELETED from config.json on the next unrelated save — no longer
    applies, because there is no hand-written dict here any more. asdict()
    enumerates the dataclass, so every field round-trips by construction and a
    new key cannot be forgotten. Keep it that way: do not go back to listing
    fields by hand for the sake of controlling key order (the order asdict gives
    IS declaration order).
    """
    config_path = get_app_dir() / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2)
