"""Global keyboard shortcut and typing into other apps (dictation).

* :class:`GlobalHotkey` registers a system-wide shortcut (Ctrl+Space by
  default) with ``RegisterHotKey``, so Jarvis can be called from any program.
* :class:`ForegroundTracker` remembers the last window you were working in
  that is not Jarvis, so dictated text goes there and not into Jarvis itself.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
SHORTCUTS = {
    "ctrl+espacio": (MOD_CONTROL, 0x20),
    "ctrl+alt+j": (MOD_CONTROL | MOD_ALT, ord("J")),
    "ctrl+shift+espacio": (MOD_CONTROL | MOD_SHIFT, 0x20),
}


class GlobalHotkey:
    """Calls *callback* whenever the shortcut is pressed, in any program."""

    def __init__(self, shortcut: str, callback: Callable[[], None]) -> None:
        self.shortcut = shortcut
        self.callback = callback
        self._thread_id = 0
        self.ok = False

    def start(self) -> None:
        if sys.platform != "win32" or self.shortcut not in SHORTCUTS:
            return
        ready = threading.Event()
        threading.Thread(
            target=self._run, args=(ready,), name="jarvis-hotkey", daemon=True
        ).start()
        ready.wait(2)

    def stop(self) -> None:
        if self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)  # type: ignore[attr-defined]

    def _run(self, ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()  # type: ignore[attr-defined]
        mods, vk = SHORTCUTS[self.shortcut]
        self.ok = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
        ready.set()
        if not self.ok:
            logger.warning("El atajo %s ya lo usa otro programa", self.shortcut)
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001
                        logger.exception("Atajo de teclado")
        finally:
            user32.UnregisterHotKey(None, 1)


class ForegroundTracker:
    """Remembers the last foreground window that is not Jarvis."""

    def __init__(self, own_title: str = "J.A.R.V.I.S") -> None:
        self.own_title = own_title
        self.hwnd = 0
        self.title = ""
        self._stop = threading.Event()

    def start(self) -> None:
        if sys.platform == "win32":
            threading.Thread(
                target=self._run, name="jarvis-foreground", daemon=True
            ).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        while not self._stop.wait(0.4):
            hwnd = user32.GetForegroundWindow()
            if not hwnd or hwnd == self.hwnd:
                continue
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            title = buf.value
            if title and not title.startswith(self.own_title):
                self.hwnd, self.title = hwnd, title

    def type_text(self, text: str) -> str:
        """Paste *text* into the remembered window (Unicode-safe)."""
        if sys.platform != "win32":
            raise RuntimeError("El dictado solo está disponible en Windows.")
        if not self.hwnd:
            raise RuntimeError(
                "No sé en qué ventana escribir: haz clic donde quieras escribir."
            )
        import ctypes

        import pyautogui

        from openjarvis.hud.computer import set_clipboard

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)  # ALT tap lets us take the focus
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(0.25)
        set_clipboard(text)
        pyautogui.hotkey("ctrl", "v")
        return f"Escrito en «{self.title[:40]}»."


def dictation_text(heard: str) -> Optional[str]:
    """Turn spoken punctuation into symbols; None if it is the stop command."""
    import re
    import unicodedata

    folded = unicodedata.normalize("NFKD", heard.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c)).strip(" .!")
    if re.fullmatch(
        r"(?:jarvis,? )?(?:fin del dictado|termina(?:r)? (?:el )?dictado|"
        r"para de escribir|deja de escribir|ya esta|stop dictado|fin de dictado)",
        folded,
    ):
        return None
    text = heard.strip()
    for spoken, mark in (
        (r"\s*\bpunto y aparte\b", ".\n"),
        (r"\s*\bnueva linea\b|\s*\bnueva línea\b", "\n"),
        (r"\s*\bpunto y coma\b", ";"),
        (r"\s*\bdos puntos\b", ":"),
        (r"\s*\bsigno de interrogacion\b|\s*\bsigno de interrogación\b", "?"),
        (r"\s*\bcoma\b", ","),
    ):
        text = re.sub(spoken, mark, text, flags=re.I)
    if text and text[-1] not in ".,;:?!\n":
        text += " "
    elif text and text[-1] in ".,;:?!":
        text += " "
    return text


__all__ = ["SHORTCUTS", "ForegroundTracker", "GlobalHotkey", "dictation_text"]
