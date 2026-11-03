"""Show, hide and find the Jarvis window (an Edge/Chrome app window).

The window's title is the page title, ``J.A.R.V.I.S.``. Hiding it (instead of
closing it) keeps the page alive, so waking Jarvis up is instant and the page
can keep listening for "Jarvis, enciéndete" in the background.
"""

from __future__ import annotations

import logging
import re
import sys
import unicodedata
from typing import List, Optional

logger = logging.getLogger(__name__)

TITLE = "J.A.R.V.I.S"

SW_HIDE, SW_SHOWNORMAL, SW_MINIMIZE, SW_RESTORE, SW_SHOW = 0, 1, 6, 9, 5


def _windows(include_hidden: bool = True) -> List[int]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd: int, _lparam: int) -> bool:
        if not include_hidden and not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.startswith(TITLE):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True

    user32.EnumWindows(_each, 0)
    return found


def find() -> Optional[int]:
    wins = _windows()
    return wins[0] if wins else None


def is_visible() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    hwnd = find()
    return bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))  # type: ignore[attr-defined]


def _show(hwnd: int, cmd: int) -> None:
    import ctypes

    ctypes.windll.user32.ShowWindow(hwnd, cmd)  # type: ignore[attr-defined]


def hide() -> bool:
    """Hide the window completely (no taskbar button). The page keeps running."""
    hwnd = find()
    if hwnd:
        _show(hwnd, SW_HIDE)
    return bool(hwnd)


def minimize() -> bool:
    hwnd = find()
    if hwnd:
        _show(hwnd, SW_MINIMIZE)
    return bool(hwnd)


def bring_to_front() -> bool:
    """Show and focus the window. False if there is no window to show."""
    hwnd = find()
    if not hwnd:
        return False
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    _show(hwnd, SW_SHOW)
    _show(hwnd, SW_RESTORE)
    # Windows only lets the foreground app hand over focus; an ALT tap
    # unlocks SetForegroundWindow for a background process.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    return True


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


_WAKE = re.compile(
    r"\b(?:jarvis|yarvis|jarbis|harvis|charlie)\b.{0,25}"
    r"\b(?:enciende(?:te)?|despierta(?:te)?|activate|arriba|vuelve|estas ahi)\b"
    r"|\b(?:enciende(?:te)?|despierta(?:te)?)\b.{0,10}\b(?:jarvis|yarvis|jarbis)\b"
)


def is_wake_phrase(text: str) -> bool:
    """ "Jarvis, enciéndete" (and close variants the recogniser may produce)."""
    return bool(_WAKE.search(_fold(text)))


__all__ = [
    "TITLE",
    "bring_to_front",
    "find",
    "hide",
    "is_visible",
    "is_wake_phrase",
    "minimize",
]
