"""
config.py — Loads AutoDoc configuration from config.json adjacent to the executable.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path


def get_app_dir() -> Path:
    """Returns the directory containing the executable (when frozen) or the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


@dataclass
class AutoDocConfig:
    api_key: str
    whisper_model: str = "base"
    focus_radius_px: int = 200
    blur_strength: int = 15
    sessions_dir: str = "sessions"
    output_dir: str = "output"
    claude_model: str = "claude-sonnet-5"
    max_screenshots_per_session: int = 100
    max_image_dimension: int = 1280
    # Which backend generates documentation: "anthropic" (cloud) or "local"
    # (any OpenAI-compatible local vision server — Ollama, LM Studio, etc.).
    # See generation/backends/ for the implementations.
    provider: str = "anthropic"
    local_endpoint: str = "http://localhost:11434/v1/chat/completions"
    local_model: str = "llava"


_DEFAULTS = {
    "api_key": "",
    "whisper_model": "base",
    "focus_radius_px": 200,
    "blur_strength": 15,
    "sessions_dir": "sessions",
    "output_dir": "output",
    "claude_model": "claude-sonnet-5",
    "max_screenshots_per_session": 100,
    "max_image_dimension": 1280,
    "provider": "anthropic",
    "local_endpoint": "http://localhost:11434/v1/chat/completions",
    "local_model": "llava",
}


def load_config() -> AutoDocConfig:
    config_path = get_app_dir() / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.json not found at {config_path}.\n"
            "Please place config.json next to the AutoDoc executable."
        )
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    # Merge with defaults so missing keys don't crash
    merged = {**_DEFAULTS, **{k: v for k, v in data.items() if k in _DEFAULTS}}
    return AutoDocConfig(**merged)


def write_config(cfg: AutoDocConfig) -> None:
    config_path = get_app_dir() / "config.json"
    data = {
        "api_key": cfg.api_key,
        "whisper_model": cfg.whisper_model,
        "focus_radius_px": cfg.focus_radius_px,
        "blur_strength": cfg.blur_strength,
        "sessions_dir": cfg.sessions_dir,
        "output_dir": cfg.output_dir,
        "claude_model": cfg.claude_model,
        "max_screenshots_per_session": cfg.max_screenshots_per_session,
        "max_image_dimension": cfg.max_image_dimension,
        "provider": cfg.provider,
        "local_endpoint": cfg.local_endpoint,
        "local_model": cfg.local_model,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
