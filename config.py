"""
config.py — Loads AutoDoc configuration from config.json adjacent to the executable.
"""

import json
import sys
from dataclasses import dataclass, field
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
    provider: str = "anthropic"
    local_endpoint: str = "http://localhost:11434/v1/chat/completions"
    local_model: str = "gemma4-e4b-fast"


_DEFAULTS = {
    "api_key": "",
    "whisper_model": "base",
    "focus_radius_px": 200,
    "blur_strength": 15,
    "screenshot_mode": "raw",
    "circle_radius_px": 36,
    "output_formats": ["docx", "html", "md"],
    "manual_capture_hotkey": "<ctrl>+<shift>+s",
    "pause_hotkey": "<ctrl>+<alt>+p",
    "stop_hotkey": "<ctrl>+<shift>+e",
    "sessions_dir": "sessions",
    "output_dir": "output",
    "claude_model": "claude-sonnet-5",
    "max_screenshots_per_session": 100,
    "max_image_dimension": 1280,
    "provider": "anthropic",
    "local_endpoint": "http://localhost:11434/v1/chat/completions",
    "local_model": "gemma4-e4b-fast",
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
        "screenshot_mode": cfg.screenshot_mode,
        "circle_radius_px": cfg.circle_radius_px,
        "output_formats": cfg.output_formats,
        "manual_capture_hotkey": cfg.manual_capture_hotkey,
        "pause_hotkey": cfg.pause_hotkey,
        "stop_hotkey": cfg.stop_hotkey,
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
