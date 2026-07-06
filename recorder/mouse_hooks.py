"""
recorder/mouse_hooks.py — Global mouse click listener.

Uses pynput to install a system-wide mouse hook. The listener runs in its own
thread (pynput manages this internally). Callbacks are invoked on button
*release* to ensure the target window has fully reacted to the click before
the screenshot is taken.

NOTE: On Windows, global hooks may trigger AV/SmartScreen warnings. The
application should be code-signed before distribution.
"""

from typing import Callable

from pynput import mouse


class MouseHooks:
    def __init__(
        self,
        on_left_click: Callable[[int, int], None],
        on_right_click: Callable[[int, int], None],
    ) -> None:
        self._on_left = on_left_click
        self._on_right = on_right_click
        self._listener: mouse.Listener | None = None

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        # Fire on release so the UI has settled before we screenshot.
        if pressed:
            return
        if button == mouse.Button.left:
            self._on_left(x, y)
        elif button == mouse.Button.right:
            self._on_right(x, y)

    def start(self) -> None:
        """Start the listener in a pynput-managed daemon thread."""
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()
        self._listener.wait()  # Block until the hook is installed.

    def stop(self) -> None:
        """Uninstall the hook and clean up."""
        if self._listener:
            self._listener.stop()
            self._listener = None
