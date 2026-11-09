"""Thread seam: worker threads ask for UI work with call_soon(fn).

Widgets may only be touched from the thread that created QApplication. Violating
that rarely crashes on the spot — it corrupts paint state or crashes later
somewhere unrelated (HANDOFF.md invariant #1).
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
        # QueuedConnection is the whole point: it makes emit() from a worker post an
        # event to THIS object's thread instead of running the slot inline.
        self.invoke.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            # A failing UI callback must never take down the event loop.
            log.exception("ui_service: queued UI callback raised")


def init() -> None:
    """Create the marshaller: main thread only, after QApplication and before any worker."""
    global _marshal
    if _marshal is not None:
        return
    _marshal = _Marshal()
    log.info("ui_service: marshaller ready on main thread")


def call_soon(fn: Callable[[], None]) -> None:
    """Schedule fn() on the Qt main thread from any thread; the only sanctioned route."""
    if _marshal is None:
        log.warning("ui_service.call_soon: not initialised; dropping callback %r", fn)
        return
    _marshal.invoke.emit(fn)


def set_main_window(window) -> None:
    global _main_window
    _main_window = window


def get_main_window():
    """The app's main window, or None. For use as a dialog parent only."""
    return _main_window
