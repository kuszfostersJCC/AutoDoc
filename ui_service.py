"""
ui_service.py — the thread seam between AutoDoc's worker threads and its Qt UI.

WHY THIS STILL EXISTS AFTER THE QT PORT (2026-07-24):
Under tkinter this module existed to enforce "exactly one Tk root, on one
thread", because touching a Tcl interpreter from the wrong thread aborts the
process at the native level (Tcl_Panic — uncatchable from Python, empty log,
tray icon just vanishes). Qt has the same underlying constraint with a
different failure mode: QWidget and everything that touches one may only be
used from the thread that created QApplication (the main thread). Violating it
doesn't reliably crash — it corrupts paint state, deadlocks, or crashes
*later* somewhere unrelated, which is strictly harder to debug than the Tcl
abort was.

So the rule is unchanged in spirit and the public API is unchanged on purpose:

    worker thread  ──▶  ui_service.call_soon(fn)  ──▶  fn() runs on main thread

Every existing worker-thread call site (doc generation, retranscribe, update
check/download, model-list fetch) keeps working verbatim. Nothing in
recorder/, transcription/, or generation/ imports Qt or knows the UI exists.

HOW IT WORKS: a single _Marshal QObject lives on the main thread. call_soon()
emits its `invoke` signal carrying the callable. Because the connection is
Qt.QueuedConnection, Qt posts the call to the *receiver's* thread affinity
(the main thread) and returns immediately — this is the sanctioned way to
cross threads in Qt, and emitting a signal from a non-main thread is safe.
The queue draining is Qt's own event loop; there is no polling timer like the
old root.after(30ms) pump.

WHAT CHANGED FROM THE TK VERSION:
  * start()/stop() are gone — QApplication owns the loop now, and it lives on
    the process main thread (tray/tray_app.py no longer runs a second pump;
    pystray is gone entirely).
  * get_root() is gone. Nothing should reach for a global parent window; pass
    an explicit parent, or use get_main_window() when a dialog genuinely needs
    the app's main window as parent.
  * scale()/scaled_geometry() are gone. Qt does its own DPI scaling, so
    widget sizes are authored in logical pixels and need no manual factor.
    NOTE: this does NOT apply to screenshot/click coordinates — those are
    physical pixels handled in recorder/ via mss+pynput and are unaffected by
    Qt. See main.py::_enable_windows_dpi_awareness for why ordering matters.
"""

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal

log = logging.getLogger(__name__)

_marshal: Optional["_Marshal"] = None
_main_window = None


class _Marshal(QObject):
    """Carries callables from any thread onto the thread that owns this object."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        # QueuedConnection is the whole point: it makes emit() from a worker
        # thread post an event to THIS object's thread rather than running the
        # slot inline on the caller's thread. A DirectConnection here would
        # silently reintroduce exactly the cross-thread widget access this
        # module exists to prevent.
        self.invoke.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            # A failing UI callback must never take down the event loop.
            log.exception("ui_service: queued UI callback raised")


def init() -> None:
    """
    Create the marshaller. MUST be called from the main thread, after
    QApplication exists and before any worker thread can call call_soon().
    """
    global _marshal
    if _marshal is not None:
        return
    _marshal = _Marshal()
    log.info("ui_service: marshaller ready on main thread")


def call_soon(fn: Callable[[], None]) -> None:
    """
    Schedule fn() to run on the Qt main thread. Safe to call from any thread.
    This is the ONLY way non-main threads may cause UI work to happen.
    """
    if _marshal is None:
        log.warning("ui_service.call_soon: not initialised; dropping callback %r", fn)
        return
    _marshal.invoke.emit(fn)


def set_main_window(window) -> None:
    """Record the app's main window so dialogs can parent to it."""
    global _main_window
    _main_window = window


def get_main_window():
    """
    The app's main window, or None if it isn't open.

    Only for use as a dialog parent. Do not reach through this to poke at
    another window's widgets from a worker thread — that's what call_soon is
    for.
    """
    return _main_window
