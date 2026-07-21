"""
ui_service.py — Single shared Tk root + dedicated UI thread for the whole app.

WHY THIS EXISTS (read before "simplifying" back to multiple Tk() roots):
Before 2026-07-08 the app created a separate tk.Tk() root — each with its own
Tcl interpreter and mainloop — in a separate daemon thread for the naming
dialog, the Sessions window, and the Session editor. Three Tcl interpreters
across three threads. Tcl aborts the whole process (Tcl_Panic — a hard native
crash that no Python except/excepthook/log handler can see) when interpreter
state is touched from the wrong thread. The most common trigger: any
tkinter.messagebox call WITHOUT an explicit parent= — it binds to the
process-wide "default root", i.e. whichever Tk() happened to be created first,
which usually lives in a DIFFERENT thread. Observed symptom: clicking
"Generate Documentation" without a working backend showed the (correctly
caught) error via messagebox → whole app vanished, tray icon and all, with an
empty log.

The fix is architectural, not a guard clause:
  * exactly ONE hidden Tk root, owned by ONE dedicated UI thread (started here)
  * every window in the app is a Toplevel of that root
  * every messagebox/dialog passes an explicit parent
  * other threads NEVER touch Tk directly — they hand callables to
    call_soon(), which a root.after() pump running inside the UI thread
    drains. Even root.after() itself is never invoked cross-thread.

pystray keeps the process's main thread (its Win32 backend requires that);
this UI thread is the only other thread allowed to run Tk code.
"""

import logging
import queue
import threading
import tkinter as tk
from typing import Callable, Optional

log = logging.getLogger(__name__)

_PUMP_INTERVAL_MS = 30

_root: Optional[tk.Tk] = None
_ready = threading.Event()
_thread: Optional[threading.Thread] = None
_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()

# UI scale factor: real DPI / 96. Set once when the root is created (requires
# the DPI awareness declared in main.py — without it Windows lies about DPI).
_scale = 1.0


def start() -> None:
    """Start the UI thread and block until the hidden root exists (max 10s)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, name="tk-ui", daemon=True)
    _thread.start()
    if not _ready.wait(timeout=10):
        log.error("ui_service.start: UI thread did not become ready within 10s")


def _run() -> None:
    global _root
    _root = tk.Tk()
    _root.withdraw()  # The shared root itself is never shown.
    _apply_dpi_scaling(_root)
    _root.after(_PUMP_INTERVAL_MS, _pump)
    _ready.set()
    log.info("ui_service: UI thread running")
    _root.mainloop()
    log.info("ui_service: UI thread mainloop exited")


def _apply_dpi_scaling(root: tk.Tk) -> None:
    """
    Size the UI for the actual display DPI.

    winfo_fpixels('1i') reports true pixels-per-inch only because main.py
    declared DPI awareness before any window existed. Three knobs, all
    needed: 'tk scaling' (point-sized fonts), explicit resize of Tk's named
    fonts (on Windows they're pixel-sized and IGNORE tk scaling — the reason
    naive fixes don't work), and ttk Treeview row height (also pixel-based).
    Window geometries are handled by callers via scale().
    """
    global _scale
    try:
        dpi = root.winfo_fpixels("1i")
        _scale = max(1.0, dpi / 96.0)
        root.tk.call("tk", "scaling", dpi / 72.0)
        log.info("ui_service: display dpi=%.0f scale=%.2f", dpi, _scale)

        if _scale > 1.05:
            from tkinter import font as tkfont
            from tkinter import ttk

            for name in (
                "TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                "TkTooltipFont", "TkFixedFont", "TkIconFont", "TkCaptionFont",
            ):
                try:
                    f = tkfont.nametofont(name, root=root)
                    f.configure(size=int(f.cget("size") * _scale))
                except tk.TclError:
                    pass
            ttk.Style(root).configure("Treeview", rowheight=int(20 * _scale))
    except Exception:
        _scale = 1.0
        log.exception("ui_service: DPI scaling failed — using 1.0")


def scale(px: int) -> int:
    """Scale a 96-DPI pixel measurement to the actual display."""
    return int(px * _scale)


def scaled_geometry(width: int, height: int) -> str:
    """Tk geometry string for a window designed at 96 DPI."""
    return f"{scale(width)}x{scale(height)}"


def _pump() -> None:
    """Drain queued callables. Runs inside the UI thread via root.after."""
    while True:
        try:
            fn = _queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception:
            # A failing UI callback must never kill the pump.
            log.exception("ui_service: queued UI callback raised")
    if _root is not None:
        _root.after(_PUMP_INTERVAL_MS, _pump)


def call_soon(fn: Callable[[], None]) -> None:
    """
    Schedule fn() to run on the UI thread. Safe to call from any thread.
    This is the ONLY way non-UI threads may cause UI work to happen.
    """
    if not _ready.is_set():
        log.warning("ui_service.call_soon: UI thread not started; dropping callback %r", fn)
        return
    _queue.put(fn)


def get_root() -> Optional[tk.Tk]:
    """The shared hidden root. Only touch it from code already on the UI thread."""
    return _root


def stop() -> None:
    """Ask the UI thread to exit its mainloop (used on app quit)."""
    if _ready.is_set() and _root is not None:
        _queue.put(_root.quit)
