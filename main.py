"""
main.py — AutoDoc entry point.

Responsibilities:
1. Ensure config.json exists next to the executable.
2. If the API key is blank AND we're running from source, prompt the user
   once with a simple tkinter dialog (admin/dev convenience only).
3. Load the validated config and hand off to the TrayApp.

Important: recording and transcription are fully local and need no API key
or provider at all. Only "Generate Documentation" needs a working backend.
A missing/invalid key must never block the app from launching — it should
only surface as a friendly error at the moment someone actually clicks
Generate (see generation/backends/ and sessions_window.py's error handling).
"""

import json
import logging
import platform
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog

from config import AutoDocConfig, get_app_dir, load_config, write_config


def _setup_logging() -> None:
    """
    Write a plain-text log (autodoc.log, beside the exe) recording timing for
    slow/opaque steps like Whisper model loading and transcription. There's
    no console in the packaged build (console=False), so this is the only
    way to see what actually happened on a machine we can't watch directly —
    e.g. distinguishing "antivirus is scanning a freshly copied model file"
    from "silently re-downloading it" from "just a slow disk".
    """
    log_path = get_app_dir() / "autodoc.log"
    logging.basicConfig(
        filename=str(log_path),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)
    log.info("=== AutoDoc starting (frozen=%s) ===", getattr(sys, "frozen", False))
    # Environment fingerprint — cheap, and exactly the details needed when a
    # build behaves differently on a machine we can't watch (dev laptop vs.
    # clean target PC, local disk vs. flash drive, OS build differences).
    log.info(
        "env: python=%s | os=%s | machine=%s | executable=%s",
        sys.version.split()[0],
        platform.platform(),
        platform.machine(),
        sys.executable,
    )


def _enable_windows_dpi_awareness() -> None:
    """
    Declare Per-Monitor V2 DPI awareness BEFORE any window exists.

    Two effects, both required:
    1. UI scaling: with DPI awareness on, ui_service measures the real DPI
       and scales Tk fonts/geometry — otherwise a 4K display at 150-200%
       renders the whole app tiny (reported 2026-07-14).
    2. Coordinate correctness: without awareness, Windows *virtualizes*
       coordinates for this process — pynput click positions and Win32
       WindowFromPoint would disagree with mss's physical-pixel captures on
       scaled displays, silently offsetting blur/circle positions and
       click-target lookups. Awareness makes everything physical pixels.

    Fallback chain: Per-Monitor V2 (Win10 1703+) → Per-Monitor (Win 8.1+) →
    system DPI aware (legacy). All failures are non-fatal.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # S_OK
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _install_crash_visibility() -> None:
    """
    Route otherwise-invisible failures into autodoc.log.

    In a console=False frozen build, sys.stderr is None/discarded. Python
    reports unhandled exceptions in *threads* by writing to stderr via
    threading.excepthook — so a background thread (e.g. the Whisper
    preloader) that dies from an exception its own try/except doesn't cover
    vanishes with ZERO trace in our log. This is precisely the failure mode
    observed on 2026-07-08: _preload_whisper logged its first line, then the
    thread died silently somewhere inside `import whisper`, and the only
    symptom was the 300s model-wait timeout. These hooks make any such death
    show up as a CRITICAL log line with a full traceback.
    """
    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("threading").critical(
            "UNHANDLED EXCEPTION in thread %r — this thread is now dead.",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook

    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        logging.getLogger(__name__).critical(
            "UNHANDLED EXCEPTION in main thread — process is about to exit.",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _main_hook

    # In a console=False build sys.stdout/sys.stderr are None, and any library
    # that writes to them crashes with AttributeError: 'NoneType' has no
    # attribute 'write'. Observed 2026-07-08: whisper's tqdm progress bar did
    # exactly that inside model.transcribe(). Give them a harmless sink so no
    # third-party print/progress-bar can ever take down a worker thread.
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


# ------------------------------------------------------------------ #
# Default config                                                       #
# ------------------------------------------------------------------ #

_DEFAULT_CONFIG = {
    "api_key": "",
    "whisper_model": "base",
    "focus_radius_px": 200,
    "blur_strength": 15,
    "screenshot_mode": "blurred",
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
    "local_model": "llava",
}


# ------------------------------------------------------------------ #
# First-run setup (source/dev runs only)                               #
# ------------------------------------------------------------------ #

def _prompt_for_api_key() -> str | None:
    """
    Show a one-time dialog asking for the Anthropic API key.
    Returns the entered key, or None if the user cancelled.

    Only shown when running from source (not a packaged build) — this is
    a developer/admin setup step, not something an end user should see.
    Packaged builds should ship with a real key already in config.json;
    if one isn't there, the app still launches fine (see main()) and
    Generate Documentation will just show a friendly error until it is.
    """
    root = tk.Tk()
    root.withdraw()  # Hide the blank root window.

    messagebox.showinfo(
        "AutoDoc — Setup",
        "Welcome to AutoDoc!\n\n"
        "To generate documentation, an Anthropic API key is required.\n"
        "Please enter it below. It will be stored in config.json next to\n"
        "this application and is never transmitted anywhere except to the\n"
        "Anthropic API when you generate a document.\n\n"
        "You can skip this — recording and transcription work without it.",
    )

    key = simpledialog.askstring(
        "AutoDoc — API Key",
        "Paste your Anthropic API key (or leave blank to skip):",
        show="*",
        parent=root,
    )
    root.destroy()
    return key.strip() if key else None


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    _enable_windows_dpi_awareness()  # must precede ANY window/Tk creation
    _setup_logging()
    _install_crash_visibility()
    config_path = get_app_dir() / "config.json"
    is_frozen = getattr(sys, "frozen", False)

    # Create config.json with defaults if it doesn't exist yet.
    if not config_path.exists():
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, indent=2)

    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)

    provider = (raw.get("provider") or "anthropic").strip().lower()

    # Only prompt when running from source — an admin/dev setup convenience.
    # Packaged builds NEVER block startup or nag an end user about missing
    # credentials: recording and transcription are fully local and work with
    # no key at all, and Generate Documentation fails gracefully in place
    # (see sessions_window.py / session_editor.py) if the backend isn't
    # configured or the key is rejected. If you're seeing this prompt on a
    # coworker's machine, config.json didn't get copied/filled in correctly
    # when the app was packaged — check dist/AutoDoc/config.json.
    if not is_frozen and provider == "anthropic" and not raw.get("api_key", "").strip():
        key = _prompt_for_api_key()
        if key:
            raw["api_key"] = key
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
        # No key entered — proceed anyway. Recording/transcription still work.

    try:
        config = load_config()
    except Exception as exc:
        # Show a user-friendly error if config is malformed. This is a real
        # blocker (the file itself can't be read), unlike a merely-missing
        # API key above, so it's fine to stop here.
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
