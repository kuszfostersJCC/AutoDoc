"""Loads AutoDoc configuration from config.json beside the executable.

AutoDocConfig is the single source of truth; default_config() and write_config()
derive from its fields. The defaults below are the JCC deployment baseline: they
ship in _internal/ and so reach a machine on the next update, but only fill keys
ABSENT from that machine's config.json.
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
    whisper_model: str = "base"
    focus_radius_px: int = 200
    blur_strength: int = 15
    # How a stored (always raw) screenshot is rendered: "raw", "blurred" (clear
    # circle at the click), or "circled" (red circle marking the click).
    screenshot_mode: str = "circled"
    circle_radius_px: int = 36
    # Any subset of "md", "html", "docx"; the first is what the "Saved:" dialog opens.
    output_formats: list = field(default_factory=lambda: ["docx", "html", "md"])
    # pynput GlobalHotKeys syntax, active only while recording; empty disables.
    manual_capture_hotkey: str = "<ctrl>+<shift>+s"
    # Not ctrl+shift+p — that is the print dialog in browsers and Office.
    pause_hotkey: str = "<ctrl>+<alt>+p"
    stop_hotkey: str = "<ctrl>+<shift>+e"
    # Device NAME, not a PortAudio index (indices shift when hardware changes).
    # Empty = the Windows default input device.
    audio_device: str = ""
    sessions_dir: str = "sessions"
    output_dir: str = "output"
    max_screenshots_per_session: int = 100
    max_image_dimension: int = 1280
    # Must be port 8080, the metering proxy: 8081 answers correctly but its
    # tokens are invisible to every metric on the server.
    local_endpoint: str = "http://172.20.28.203:8080/v1/chat/completions"
    # Must be a model id the server serves. Only one is resident at a time, so a
    # different value here can trigger a cold model swap on every generation.
    local_model: str = "gemma4-e4b-fast"
    # A metering label, not access control: a blank value still generates fine but
    # folds AutoDoc's usage into aggregate staff chat, with no visible symptom.
    local_api_key: str = "sk-nuPkxLzZ3YFETKOCwhvCXRPjdWB4Ip_J"
    # The bot token is deliberately NOT a config key (write_config() rewrites the
    # whole file, so a Settings save would wipe it); see publish/token.py.
    bookstack_enabled: bool = False
    bookstack_base_url: str = "http://jcbookstack.jccmi.edu"
    bookstack_book: str = "AutoDoc"
    bookstack_timeout_s: int = 30
    # Validated against publish/tags.py; an empty string means "don't apply this tag".
    bookstack_tag_doc_type: str = "sop"
    bookstack_tag_owner: str = "it-ops"
    bookstack_tag_audience: str = "all-staff"
    bookstack_tag_review_cycle: str = "ad-hoc"
    # "light" | "dark", validated by theme/tokens.py::resolve_theme().
    theme: str = "light"


def default_config() -> dict:
    """A fresh dict of every config key at its default; mutable defaults are new objects."""
    out = {}
    for f in fields(AutoDocConfig):
        if f.default_factory is not MISSING:      # a field with a factory has default=MISSING
            out[f.name] = f.default_factory()
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

    # Defaults fill absent keys and also act as the whitelist: an unrecognised key
    # in config.json is dropped rather than raising in AutoDocConfig(**merged).
    defaults = default_config()
    merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
    return AutoDocConfig(**merged)


def write_config(cfg: AutoDocConfig) -> None:
    """
    Rewrite config.json in FULL from the in-memory config (HANDOFF.md invariant #9),
    so a hand-edit made while AutoDoc is open is discarded by the next Settings save.
    """
    config_path = get_app_dir() / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2)
