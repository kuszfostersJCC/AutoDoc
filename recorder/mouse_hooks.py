"""Global mouse click listener (pynput)."""

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
        # Press, not release: a screenshot documents what was clicked, so it has
        # to be taken before the target window reacts.
        if not pressed:
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
        if self._listener:
            self._listener.stop()
            self._listener = None
