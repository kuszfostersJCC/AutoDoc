"""
main.py — AutoDoc entry point.

Responsibilities:
1. Ensure config.json exists next to the executable.
2. If the API key is blank, prompt the user once with a simple tkinter dialog.
3. Load the validated config and hand off to the TrayApp.
"""

import json
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog

from config import AutoDocConfig, get_app_dir, load_config, write_config


# ------------------------------------------------------------------ #
# Default config                                                       #
# ------------------------------------------------------------------ #

_DEFAULT_CONFIG = {
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


# ------------------------------------------------------------------ #
# First-run setup                                                      #
# ------------------------------------------------------------------ #

def _prompt_for_api_key() -> str | None:
    """
    Show a one-time dialog asking for the Anthropic API key.
    Returns the entered key, or None if the user cancelled.
    """
    root = tk.Tk()
    root.withdraw()  # Hide the blank root window.

    messagebox.showinfo(
        "AutoDoc — Setup",
        "Welcome to AutoDoc!\n\n"
        "To generate documentation, an Anthropic API key is required.\n"
        "Please enter it below. It will be stored in config.json next to\n"
        "this application and is never transmitted anywhere except to the\n"
        "Anthropic API when you generate a document.",
    )

    key = simpledialog.askstring(
        "AutoDoc — API Key",
        "Paste your Anthropic API key:",
        show="*",
        parent=root,
    )
    root.destroy()
    return key.strip() if key else None


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    config_path = get_app_dir() / "config.json"

    # Create config.json with defaults if it doesn't exist yet.
    if not config_path.exists():
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, indent=2)

    # If no API key is set, walk the user through setup.
    # Only relevant for the "anthropic" provider — a local backend needs no key.
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)

    provider = (raw.get("provider") or "anthropic").strip().lower()

    if provider == "anthropic" and not raw.get("api_key", "").strip():
        key = _prompt_for_api_key()
        if not key:
            # User cancelled — exit cleanly.
            sys.exit(0)
        raw["api_key"] = key
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)

    try:
        config = load_config()
    except Exception as exc:
        # Show a user-friendly error if config is malformed.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "AutoDoc — Configuration Error",
            f"Failed to load config.json:\n\n{exc}\n\n"
            "Please check the file next to AutoDoc.exe.",
        )
        root.destroy()
        sys.exit(1)

    import signal

    from tray.tray_app import TrayApp

    app = TrayApp(config)

    # Allow Ctrl+C from the terminal to exit cleanly during development.
    # pystray's Win32 message loop doesn't propagate SIGINT on its own.
    def _handle_sigint(sig, frame):
        app.quit()

    signal.signal(signal.SIGINT, _handle_sigint)

    app.run()


if __name__ == "__main__":
    main()
