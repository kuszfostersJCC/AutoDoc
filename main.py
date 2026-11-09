"""AutoDoc entry point: DPI awareness, QApplication, theme, config, then the tray app.

Recording and transcription are fully local; only "Generate Documentation" needs the
AI server, so an unreachable server must never block startup.
"""

import json
import logging
import platform
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

import ui_service
from config import default_config, get_app_dir, load_config
from theme import apply_theme


def _setup_logging() -> None:
    """Set up autodoc.log beside the exe — the packaged build has no console."""
    log_path = get_app_dir() / "autodoc.log"
    logging.basicConfig(
        filename=str(log_path),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)
    log.info("=== AutoDoc starting (frozen=%s) ===", getattr(sys, "frozen", False))
    log.info(
        "env: python=%s | os=%s | machine=%s | executable=%s",
        sys.version.split()[0],
        platform.platform(),
        platform.machine(),
        sys.executable,
    )


def _enable_windows_dpi_awareness() -> None:
    """
    Declare Per-Monitor V2 DPI awareness. MUST run before QApplication(), which sets
    process awareness itself if still unset — and it cannot be changed afterwards.

    Without awareness Windows virtualizes this process's coordinates, so pynput click
    positions and Win32 lookups no longer agree with mss's physical-pixel captures on
    a scaled display, silently offsetting blur/circle positions.
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
    Give AutoDoc its own Windows AppUserModelID, before any window is created:
    otherwise the taskbar button belongs to the host process (python.exe from source)
    and shows that process's icon whatever setWindowIcon() says.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JCC.AutoDoc")
    except Exception:
        pass  # cosmetic only — never block startup over a taskbar icon


def _app_icon() -> QIcon:
    """
    The application icon from assets/. A multi-resolution .ico is preferred over a
    PNG: Windows asks for 16-256px variants and rescales a single-size PNG badly.
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

    In a console=False frozen build sys.stderr is None, so Python's default report for
    an unhandled exception in a worker thread is discarded and the thread dies with no
    trace at all.
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

    # Same build has sys.stdout/sys.stderr as None, so any library writing to them
    # raises AttributeError (whisper's tqdm did). Give them a harmless sink.
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main() -> None:
    _enable_windows_dpi_awareness()  # must precede QApplication — see docstring
    _set_windows_app_id()
    _setup_logging()
    _install_crash_visibility()

    app = QApplication(sys.argv)
    # Tray app: closing the Sessions window must not quit Qt mid-recording.
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("AutoDoc")
    app.setWindowIcon(_app_icon())
    ui_service.init()        # thread seam; main thread only, after QApplication

    config_path = get_app_dir() / "config.json"

    if not config_path.exists():
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config(), f, indent=2)

    # Tolerate a malformed file: it must still reach the error dialog below,
    # unstyled if need be, rather than dying here with only a log line.
    raw = {}
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        logging.getLogger(__name__).exception("could not read config.json for theming")

    # Theming is pinned here: the theme is a config value, so it cannot happen before
    # `raw` exists, and it cannot happen after load_config(), whose failure path shows
    # a QMessageBox that would otherwise be unstyled. Raw JSON rather than the
    # AutoDocConfig, so a malformed config still gets a themed error dialog.
    apply_theme(app, raw.get("theme"))   # Fusion + palette + app.qss + setFont

    try:
        config = load_config()
    except Exception as exc:
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

    # Qt's event loop blocks in C++ and never yields to the interpreter, so a SIGINT
    # handler alone would never run; this no-op timer wakes Python every 200ms.
    signal.signal(signal.SIGINT, lambda *_: tray.quit())
    _sigint_timer = QTimer()
    _sigint_timer.start(200)
    _sigint_timer.timeout.connect(lambda: None)
    app._sigint_timer = _sigint_timer  # keep a reference alive

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
