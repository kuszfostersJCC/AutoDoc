"""Identify the UI element under a screen point; Windows only, best effort.

Two independent layers: pure-ctypes Win32 (window title, process name) and
optional comtypes UIA (element name, role). Either may fail and degrade quietly.
"""

import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# UIA ControlType IDs → human-readable role names (UIAutomationClient.h).
_CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combo box",
    50004: "text field", 50005: "link", 50006: "image", 50007: "list item",
    50008: "list", 50009: "menu", 50010: "menu bar", 50011: "menu item",
    50012: "progress bar", 50013: "radio button", 50014: "scroll bar",
    50015: "slider", 50016: "spinner", 50017: "status bar", 50018: "tab",
    50019: "tab item", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "tree item", 50025: "control", 50026: "group",
    50027: "thumb", 50028: "data grid", 50029: "data item", 50030: "document",
    50031: "split button", 50032: "window", 50033: "pane", 50034: "header",
    50035: "header item", 50036: "table", 50037: "title bar", 50038: "separator",
}

_uia_warned = False
_win32_warned = False


def get_click_target(x: int, y: int) -> Optional[dict]:
    """A dict of element_name, element_role, window_title and app_name (each
    possibly ""), or None when nothing at all could be determined."""
    if not _IS_WINDOWS:
        return None

    info = {"element_name": "", "element_role": "", "window_title": "", "app_name": ""}

    win = _win32_window_info(x, y)
    if win:
        info.update(win)

    elem = _uia_element_info(x, y)
    if elem:
        info.update(elem)

    return info if any(info.values()) else None


def _win32_window_info(x: int, y: int) -> Optional[dict]:
    global _win32_warned
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return None
        GA_ROOT = 2
        top = user32.GetAncestor(hwnd, GA_ROOT) or hwnd

        length = user32.GetWindowTextLengthW(top)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(top, buf, length + 1)
            title = buf.value

        app_name = ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(top, ctypes.byref(pid))
        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if handle:
                try:
                    size = wintypes.DWORD(1024)
                    path_buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                        app_name = path_buf.value.rsplit("\\", 1)[-1]
                finally:
                    kernel32.CloseHandle(handle)

        return {"window_title": title, "app_name": app_name}
    except Exception:
        if not _win32_warned:
            _win32_warned = True
            log.warning("ui_inspect: Win32 window lookup failed (further failures at DEBUG)", exc_info=True)
        else:
            log.debug("ui_inspect: Win32 window lookup failed", exc_info=True)
        return None


def _uia_element_info(x: int, y: int) -> Optional[dict]:
    global _uia_warned
    try:
        from ctypes import wintypes

        import comtypes
        import comtypes.client

        # Per-thread COM init: the capture worker thread has no COM state yet.
        try:
            comtypes.CoInitialize()
        except OSError:
            pass

        UIA = comtypes.client.GetModule("UIAutomationCore.dll")
        iuia = comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation
        )

        point = wintypes.POINT(x, y)
        element = iuia.ElementFromPoint(point)
        if element is None:
            return None

        name = (element.CurrentName or "").strip()
        role = _CONTROL_TYPES.get(element.CurrentControlType, "")

        # Truncate absurd names (some apps expose entire document text).
        if len(name) > 120:
            name = name[:117] + "..."

        return {"element_name": name, "element_role": role}
    except Exception:
        if not _uia_warned:
            _uia_warned = True
            log.warning(
                "ui_inspect: UIA element lookup unavailable — click targets will have "
                "window titles only. Is comtypes installed/bundled? "
                "(further failures at DEBUG)",
                exc_info=True,
            )
        else:
            log.debug("ui_inspect: UIA element lookup failed", exc_info=True)
        return None
