"""Hands for Jarvis: screenshots, mouse and keyboard for Claude's computer use.

Claude's ``computer_toolset_20260801`` sends one ``tool_use`` block per
action (``screenshot``, ``left_click``, ``type``…). :class:`Computer` carries
them out on the real desktop and returns what Claude needs: an image for
``screenshot`` / ``zoom`` and ``OK`` for the rest.

Coordinates: Claude works in the pixel space of the screenshots we send. They
are downscaled to fit the model's image limits, so every coordinate is mapped
back with the same factor before the mouse moves.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Image limits for the toolset models: long edge and total area.
MAX_LONG_EDGE = 1920
MAX_PIXELS = 1920 * 1200

# Claude names keys like xdotool; pyautogui wants its own names.
_KEY_NAMES = {
    "return": "enter",
    "enter": "enter",
    "kp_enter": "enter",
    "escape": "esc",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "tab": "tab",
    "space": "space",
    "page_up": "pageup",
    "page_down": "pagedown",
    "prior": "pageup",
    "next": "pagedown",
    "home": "home",
    "end": "end",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "super": "win",
    "super_l": "win",
    "meta": "win",
    "cmd": "win",
    "win": "win",
    "control": "ctrl",
    "control_l": "ctrl",
    "ctrl": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "insert": "insert",
    "caps_lock": "capslock",
    "print": "printscreen",
    "minus": "-",
    "plus": "+",
}


def key_combo(text: str) -> List[str]:
    """``"ctrl+shift+Tab"`` → ``["ctrl", "shift", "tab"]`` for pyautogui."""
    keys = []
    for part in str(text).split("+"):
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low in _KEY_NAMES:
            keys.append(_KEY_NAMES[low])
        elif low.startswith("f") and low[1:].isdigit():
            keys.append(low)
        else:
            keys.append(low if len(part) > 1 else part.lower())
    return keys


def scale_factor(width: int, height: int) -> float:
    return min(
        1.0,
        MAX_LONG_EDGE / max(width, height),
        math.sqrt(MAX_PIXELS / (width * height)),
    )


def _set_dpi_aware() -> None:
    """Make screenshots and mouse use the same (physical) pixels."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def set_clipboard(text: str) -> None:
    """Put *text* on the clipboard (Unicode-safe, unlike pyautogui.write)."""
    if sys.platform != "win32":
        import pyperclip  # type: ignore[import-not-found]

        pyperclip.copy(text)
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    u32 = ctypes.windll.user32  # type: ignore[attr-defined]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("El portapapeles está ocupado.")
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = k32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        u32.SetClipboardData(13, handle)  # CF_UNICODETEXT
    finally:
        u32.CloseClipboard()


class Computer:
    """Executes computer-use actions on this desktop."""

    def __init__(self) -> None:
        _set_dpi_aware()
        self.scale = 1.0

    # -- screen ----------------------------------------------------------

    def screenshot_b64(self, region: Optional[Tuple[int, int, int, int]] = None) -> str:
        from PIL import Image, ImageGrab

        img = ImageGrab.grab(all_screens=False)
        if region is None:
            self.scale = scale_factor(*img.size)
            self.screen_size = img.size  # physical pixels of the capture
        else:
            x0, y0, x1, y1 = (int(v / self.scale) for v in region)
            img = img.crop((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        s = scale_factor(*img.size)
        if s < 1.0:
            img = img.resize(
                (round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS
            )
        # JPEG: ~10x smaller than PNG, so each step uploads much faster.
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _xy(self, coord: Any) -> Tuple[int, int]:
        """Screenshot pixels → mouse pixels.

        With display scaling (125 % on many laptops) the capture can be in
        physical pixels while the mouse works in scaled ones; mapping through
        both sizes keeps clicks on target either way.
        """
        import pyautogui

        x, y = float(coord[0]) / self.scale, float(coord[1]) / self.scale
        mouse_w, mouse_h = pyautogui.size()
        cap_w, cap_h = getattr(self, "screen_size", (mouse_w, mouse_h))
        return round(x * mouse_w / cap_w), round(y * mouse_h / cap_h)

    # -- actions ---------------------------------------------------------

    def run(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Do one action. Returns ``{"image": b64}`` or ``{"text": ...}``."""
        import pyautogui

        pyautogui.FAILSAFE = False  # the mouse in a corner must not abort us
        pyautogui.PAUSE = 0.05
        if name == "screenshot":
            return {"image": self.screenshot_b64()}
        if name == "zoom":
            return {"image": self.screenshot_b64(tuple(args["region"]))}
        mods = key_combo(args.get("text", "")) if name.endswith("click") else []
        if name in (
            "left_click",
            "right_click",
            "middle_click",
            "double_click",
            "triple_click",
        ):
            if args.get("coordinate"):
                pyautogui.moveTo(*self._xy(args["coordinate"]))
            button = {"right_click": "right", "middle_click": "middle"}.get(
                name, "left"
            )
            clicks = {"double_click": 2, "triple_click": 3}.get(name, 1)
            for m in mods:
                pyautogui.keyDown(m)
            try:
                pyautogui.click(button=button, clicks=clicks, interval=0.06)
            finally:
                for m in reversed(mods):
                    pyautogui.keyUp(m)
        elif name == "left_click_drag":
            pyautogui.moveTo(*self._xy(args["start_coordinate"]))
            pyautogui.dragTo(*self._xy(args["coordinate"]), duration=0.4)
        elif name == "mouse_move":
            pyautogui.moveTo(*self._xy(args["coordinate"]))
        elif name == "left_mouse_down":
            pyautogui.mouseDown()
        elif name == "left_mouse_up":
            pyautogui.mouseUp()
        elif name == "cursor_position":
            x, y = pyautogui.position()
            return {"text": f"X={round(x * self.scale)}, Y={round(y * self.scale)}"}
        elif name == "scroll":
            if args.get("coordinate"):
                pyautogui.moveTo(*self._xy(args["coordinate"]))
            amount = int(args.get("scroll_amount", 3)) * 120
            direction = args.get("scroll_direction", "down")
            if direction in ("up", "down"):
                pyautogui.scroll(amount if direction == "up" else -amount)
            else:
                pyautogui.hscroll(amount if direction == "right" else -amount)
        elif name == "type":
            text = str(args.get("text", ""))
            if text.isascii() and len(text) < 40 and "\n" not in text:
                pyautogui.write(text, interval=0.01)
            else:
                set_clipboard(text)  # accents, ñ and long code paste reliably
                pyautogui.hotkey("ctrl", "v")
        elif name == "key":
            keys = key_combo(args.get("text", ""))
            for _ in range(max(1, min(100, int(args.get("repeat", 1) or 1)))):
                pyautogui.hotkey(*keys)
        elif name == "hold_key":
            keys = key_combo(args.get("text", ""))
            for k in keys:
                pyautogui.keyDown(k)
            time.sleep(min(10.0, float(args.get("duration", 1))))
            for k in reversed(keys):
                pyautogui.keyUp(k)
        elif name == "wait":
            time.sleep(min(10.0, float(args.get("duration", 1))))
        else:
            raise ValueError(f"Acción desconocida: {name}")
        time.sleep(0.15)  # let the UI react before the next screenshot
        return {"text": "OK"}


__all__ = ["Computer", "key_combo", "scale_factor", "set_clipboard"]
