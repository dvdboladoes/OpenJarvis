"""Control of Windows itself: wallpaper, volume, brightness, theme, e-mail…

Each function does one thing and returns a short Spanish sentence describing
the result, which goes back to Claude as the tool result. Anything that
changes data outside the PC (sending an e-mail) or runs arbitrary commands is
only called after the user has confirmed it out loud (see ``agent.py``).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _need_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("Esta acción solo está disponible en Windows.")


def _data_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    folder = get_config_dir() / "jarvis-archivos"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── wallpaper ───────────────────────────────────────────────────────────────

# Wikimedia asks API clients to identify themselves with a contact URL.
USER_AGENT = (
    "JarvisHUD/2.0 (https://github.com/dvdboladoes/OpenJarvis; asistente personal)"
)


def find_wallpaper_image(query: str, timeout: float = 8.0) -> Optional[str]:
    """URL of a large landscape photo for *query* (Wikimedia Commons, no key)."""
    import httpx

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": "20",
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "iiurlwidth": "2560",
    }
    try:
        resp = httpx.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Búsqueda de fondos fallida: %s", exc)
        return None
    best: tuple[int, Optional[str]] = (0, None)
    for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        for info in page.get("imageinfo") or []:
            w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            if info.get("mime") not in ("image/jpeg", "image/png") or h == 0:
                continue
            if w < 1600 or not 1.3 <= w / h <= 2.4:
                continue  # wallpapers: big and landscape
            score = 100 - int(page.get("index", 50))
            if score > best[0]:
                best = (score, info.get("thumburl") or info.get("url"))
    return best[1]


WINDOWS_DEFAULT_WALLPAPER = Path(r"C:\Windows\Web\Wallpaper\Windows\img0.jpg")


def current_wallpaper() -> str:
    _need_windows()
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
        return str(winreg.QueryValueEx(key, "WallPaper")[0] or "")


def _remember_original_wallpaper() -> None:
    """Save the user's own wallpaper the first time Jarvis changes it."""
    note = _data_dir() / "fondo-original.txt"
    try:
        now = current_wallpaper()
    except OSError:
        return
    if now and _data_dir() not in Path(now).parents:
        note.write_text(now, encoding="utf-8")


def restore_wallpaper(windows_default: bool = False) -> str:
    """Put back the user's original wallpaper (or Windows' default one)."""
    _need_windows()
    import ctypes

    note = _data_dir() / "fondo-original.txt"
    target = None
    if not windows_default and note.exists():
        saved = Path(note.read_text(encoding="utf-8").strip())
        if saved.is_file():
            target = saved
    if target is None:
        target = WINDOWS_DEFAULT_WALLPAPER
    ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, str(target), 3)  # type: ignore[attr-defined]
    if not ok:
        raise RuntimeError("Windows no aceptó el fondo.")
    which = (
        "el de Windows" if target == WINDOWS_DEFAULT_WALLPAPER else "su fondo original"
    )
    return f"Fondo restaurado: {which}."


def set_wallpaper(source: str) -> str:
    """Set the desktop background from a file path, an image URL or a topic."""
    _need_windows()
    import ctypes

    _remember_original_wallpaper()

    import httpx

    path = Path(os.path.expandvars(os.path.expanduser(source.strip('"'))))
    if not path.is_file():
        url = source if re.match(r"https?://", source) else find_wallpaper_image(source)
        if not url:
            return f"No he encontrado ninguna imagen de «{source}» para el fondo."
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()
        ext = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
        path = _data_dir() / f"fondo-{int(time.time())}{ext}"
        path.write_bytes(resp.content)
    ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)  # type: ignore[attr-defined]
    if not ok:
        raise RuntimeError("Windows no aceptó la imagen como fondo.")
    return f"Fondo de pantalla cambiado ({path.name})."


# ── sound, screen and theme ─────────────────────────────────────────────────

_VK = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}


def _key(vk: int, times: int = 1) -> None:
    import ctypes

    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)  # type: ignore[attr-defined]
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # type: ignore[attr-defined]


def set_volume(level: Optional[int] = None, change: str = "") -> str:
    """Set the volume to *level* % (0-100), or step it: ``up``/``down``/``mute``."""
    _need_windows()
    if level is not None:
        level = max(0, min(100, int(level)))
        _key(_VK["down"], 50)  # each key press is 2 %
        _key(_VK["up"], round(level / 2))
        return f"Volumen al {level} %."
    if change in _VK:
        _key(_VK[change], 1 if change == "mute" else 5)
        return {"up": "Volumen subido.", "down": "Volumen bajado."}.get(
            change, "Sonido silenciado o reactivado."
        )
    raise ValueError("Indica un nivel de volumen o up/down/mute.")


def _powershell(script: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        encoding="utf-8",
        errors="replace",
    )


def set_brightness(level: int) -> str:
    _need_windows()
    level = max(0, min(100, int(level)))
    res = _powershell(
        "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1,{level})"
    )
    if res.returncode != 0:
        return "Esta pantalla no permite cambiar el brillo desde Windows."
    return f"Brillo al {level} %."


def set_theme(dark: bool) -> str:
    _need_windows()
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        0,
        winreg.KEY_SET_VALUE,
    )
    with key:
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if dark else 1)
    return "Modo oscuro activado." if dark else "Modo claro activado."


SETTINGS_PAGES = {
    "pantalla": "display",
    "sonido": "sound",
    "wifi": "network-wifi",
    "bluetooth": "bluetooth",
    "fondo": "personalization-background",
    "colores": "personalization-colors",
    "energia": "powersleep",
    "bateria": "batterysaver",
    "actualizaciones": "windowsupdate",
    "aplicaciones": "appsfeatures",
    "notificaciones": "notifications",
    "privacidad": "privacy",
    "raton": "mousetouchpad",
    "teclado": "typing",
    "idioma": "regionlanguage",
    "fecha": "dateandtime",
    "cuentas": "yourinfo",
    "almacenamiento": "storagesense",
    "impresoras": "printers",
    "red": "network-status",
}


def open_settings(page: str) -> str:
    _need_windows()
    key = re.sub(r"[^a-z]", "", page.lower())
    target = SETTINGS_PAGES.get(key, key or "")
    os.startfile(f"ms-settings:{target}")  # type: ignore[attr-defined]  # noqa: S606
    return f"Abierta la configuración de {page or 'Windows'}."


def lock_screen() -> str:
    _need_windows()
    import ctypes

    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    return "Pantalla bloqueada."


def run_command(command: str, timeout: float = 60) -> str:
    """Run a PowerShell command (only after the user confirmed it)."""
    _need_windows()
    res = _powershell(command, timeout=timeout)
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    text = out[-1500:] if out else ""
    if res.returncode != 0:
        text += f"\n[Error {res.returncode}] {err[-600:]}"
    return text.strip() or "Hecho, sin salida."


# ── e-mail ──────────────────────────────────────────────────────────────────


def compose_email(to: str, subject: str, body: str, open_url=webbrowser.open) -> str:
    """Open a Gmail draft filled in, ready to be reviewed and sent."""
    query = urlencode(
        {"view": "cm", "fs": "1", "to": to, "su": subject, "body": body},
        quote_via=quote,
    )
    open_url(f"https://mail.google.com/mail/?{query}")
    return "Borrador abierto en Gmail."


def focus_window(title_part: str) -> bool:
    """Bring the first window whose title contains *title_part* to the front."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []
    want = title_part.lower()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if want in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_each, 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the foreground app hand focus over: a harmless ALT
    # tap makes SetForegroundWindow work from a background process.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    return True


def send_open_email(wait: float = 12.0) -> str:
    """Send the Gmail draft opened by :func:`compose_email` (Ctrl+Enter)."""
    _need_windows()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if focus_window("gmail"):
            time.sleep(0.6)
            import pyautogui

            pyautogui.hotkey("ctrl", "enter")
            return "Correo enviado."
        time.sleep(0.5)
    return "No encuentro la ventana de Gmail: envíalo tú con el botón Enviar."


__all__ = [
    "SETTINGS_PAGES",
    "compose_email",
    "find_wallpaper_image",
    "focus_window",
    "lock_screen",
    "restore_wallpaper",
    "open_settings",
    "run_command",
    "send_open_email",
    "set_brightness",
    "set_theme",
    "set_volume",
    "set_wallpaper",
]
