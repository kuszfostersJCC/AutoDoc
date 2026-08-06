"""
main.py — AutoDoc entry point.

Responsibilities:
1. Declare DPI awareness before anything creates a window (see below — this
   one is load-bearing for screenshot correctness, not just cosmetics).
2. Create the QApplication, apply the theme, start the thread seam.
3. Ensure config.json exists next to the executable.
4. If the API key is blank AND we're running from source, prompt the user
   once (admin/dev convenience only).
5. Load the validated config and hand off to the TrayApp.

Important: recording and transcription are fully local and need no API key
or provider at all. Only "Generate Documentation" needs a working backend.
A missing/invalid key must never block the app from launching — it should
only surface as a friendly error at the moment someone actually clicks
Generate (see generation/backends/ and sessions_window.py's error handling).

UI stack (2026-07-24): PySide6 / Qt Widgets. All styling is governed by
pyside6-ui-style-guide.md and lives in theme/ — see theme/__init__.py.
"""

import json
import logging
import platform
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

import ui_service
from config import default_config, get_app_dir, load_config
from theme import apply_theme


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
    Declare Per-Monitor V2 DPI awareness BEFORE QApplication is constructed.

    Two effects, both required:
    1. Coordinate correctness (the important one): without awareness, Windows
       *virtualizes* coordinates for this process — pynput click positions and
       Win32 WindowFromPoint would disagree with mss's physical-pixel captures
       on scaled displays, silently offsetting blur/circle positions and
       click-target lookups. Awareness makes all of those physical pixels.
       None of that machinery goes through Qt, so Qt cannot fix it for us.
    2. UI scaling: Qt6 scales widgets for the display DPI on its own, but it
       decides how based on the process's awareness context at the time
       QApplication is created. Setting PerMonitorV2 first means Qt inherits
       it rather than picking its own — one decision, made here, instead of
       two subsystems disagreeing.

    ORDERING IS THE WHOLE POINT: this must run before QApplication(). Qt sets
    process DPI awareness itself if it's still unset when QApplication is
    constructed, and awareness cannot be changed afterwards — so if this call
    moves below QApplication it becomes a silent no-op and the recorder's
    coordinate math regresses on any scaled display, with correct-looking
    screenshots that have the blur in the wrong place.

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


def _set_windows_app_id() -> None:
    """
    Give AutoDoc its own Windows AppUserModelID.

    Without this, Windows groups the app under whatever host process launched it
    and shows THAT process's icon in the taskbar — running from source you get
    the generic Python icon no matter what setWindowIcon() says, because the
    taskbar button belongs to python.exe's identity, not ours. Setting an
    explicit ID makes the taskbar button AutoDoc's own, so its icon and grouping
    behave correctly.

    Must run before any window is created. Frozen builds get their identity from
    the exe itself, so this mainly fixes source runs — but it's harmless and
    correct in both.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JCC.AutoDoc")
    except Exception:
        pass  # Cosmetic only — never block startup over a taskbar icon.


def _app_icon() -> QIcon:
    """
    The application icon, from assets/autodoc.ico (or a PNG fallback).

    A multi-resolution .ico is strongly preferred over a PNG: Windows asks for
    16/20/24/32/48/256px variants for the title bar, taskbar, Alt-Tab, and
    Explorer, and a single-size PNG gets rescaled into a soft mess at the small
    sizes where it's seen most.
    """
    assets = get_app_dir() / "assets"
    for name in ("autodoc.ico", "autodoc.png", "icon_idle.png"):
        path = assets / name
        if path.exists():
            return QIcon(str(path))
    logging.getLogger(__name__).warning(
        "_app_icon: no assets/autodoc.ico found — windows will use Qt's default icon"
    )
    return QIcon()


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


# NOTE: there used to be a `_DEFAULT_CONFIG` dict here, used to write the
# first-run config.json. It was deleted on 2026-07-29 — it was a hand-maintained
# second copy of config.py's defaults and had drifted from them (it shipped
# screenshot_mode "blurred" and local_model "llava", and it WON on a fresh
# install because it's what created the file). The bootstrap below now dumps
# config.default_config(), so first-run values come from the same dataclass as
# everything else and cannot disagree. Don't reintroduce a local copy.


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

    Requires QApplication to already exist (main() creates it before calling
    this). Parent is None deliberately: no window exists this early.
    """
    QMessageBox.information(
        None,
        "AutoDoc — Setup",
        "Welcome to AutoDoc!\n\n"
        "To generate documentation, an Anthropic API key is required.\n"
        "Please enter it below.",
    )

    key, ok = QInputDialog.getText(
        None,
        "AutoDoc — API Key",
        "Paste your Anthropic API key (or leave blank to skip):",
        QLineEdit.Password,
    )
    if not ok:
        return None
    return key.strip() or None


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    _enable_windows_dpi_awareness()  # MUST precede QApplication — see docstring
    _set_windows_app_id()            # also before any window exists
    _setup_logging()
    _install_crash_visibility()

    app = QApplication(sys.argv)
    # AutoDoc is a tray app: closing the Sessions window must NOT quit it.
    # Without this, Qt exits as soon as the last window closes and the tray
    # icon disappears mid-recording.
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("AutoDoc")
    # Applies to every window and dialog in the app — title bar, taskbar,
    # Alt-Tab. Individual windows never set their own icon.
    app.setWindowIcon(_app_icon())
    ui_service.init()        # thread seam; main thread only, after QApplication

    config_path = get_app_dir() / "config.json"
    is_frozen = getattr(sys, "frozen", False)

    # Create config.json with defaults if it doesn't exist yet. Those defaults
    # are the real deployment baseline (provider "local", the district AI
    # server) — see config.py's module docstring — so a fresh or reimaged
    # machine bootstraps to a config that actually works rather than to
    # "anthropic with no key".
    if not config_path.exists():
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config(), f, indent=2)

    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)

    # Theming happens HERE, not immediately after QApplication, because the
    # light/dark choice is a config value (2026-07-29) and this is the first
    # point at which config.json has been created-if-missing and read.
    #
    # The position is bounded on both sides and both bounds are real:
    #   - it cannot move earlier, because `raw` doesn't exist yet;
    #   - it cannot move later, because _prompt_for_api_key() below puts a
    #     QMessageBox and a QInputDialog on screen, and load_config()'s failure
    #     path raises a QMessageBox too. Theming after those means a source run
    #     or a broken-config run shows its first dialogs unstyled — in dark mode
    #     that's a bright white dialog in front of nothing.
    # Reads raw JSON rather than the AutoDocConfig, deliberately: a malformed
    # config must still produce a THEMED error dialog, and load_config() can't
    # be reached before that dialog exists.
    apply_theme(app, raw.get("theme"))   # Fusion + palette + app.qss + setFont

    provider = (raw.get("provider") or "anthropic").strip().lower()

    # Only prompt when running from source — an admin/dev setup convenience.
    # Packaged builds NEVER block startup or nag an end user about missing
    # credentials: recording and transcription are fully local and work with
    # no key at all, and Generate Documentation fails gracefully in place
    # (see sessions_window.py) if the backend isn't configured or the key is
    # rejected. If you're seeing this prompt on a coworker's machine,
    # config.json didn't get copied/filled in correctly when the app was
    # packaged — check dist/AutoDoc/config.json.
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
        QMessageBox.critical(
            None,
            "AutoDoc — Configuration Error",
            f"Failed to load config.json:\n\n{exc}\n\n"
            "Please check the file next to AutoDoc.exe.",
        )
        sys.exit(1)

    import signal

    from tray.tray_app import TrayApp

    tray = TrayApp(config)
    tray.start()

    # Allow Ctrl+C from the terminal to exit cleanly during development.
    # Qt's event loop blocks in C++ and never yields to the Python
    # interpreter, so a SIGINT handler alone would never run — this no-op
    # timer forces the interpreter awake every 200ms so pending signals get
    # dispatched. Without it, Ctrl+C is simply ignored until the app exits.
    signal.signal(signal.SIGINT, lambda *_: tray.quit())
    _sigint_timer = QTimer()
    _sigint_timer.start(200)
    _sigint_timer.timeout.connect(lambda: None)
    app._sigint_timer = _sigint_timer  # keep a reference alive

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
