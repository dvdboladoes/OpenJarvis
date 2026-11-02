"""Voice control of the computer: open apps, websites, YouTube, folders, media.

Commands are recognised with deterministic Spanish patterns (instant and
reliable even with a tiny local model). For safety Jarvis never runs
arbitrary shell commands: it only opens URLs, programs found in the Start
Menu (or a short list of known Windows apps), well-known folders, and sends
media keys.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


def _fold(text: str) -> str:
    """lowercase, no accents, single spaces."""
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[¿?¡!.,;:\"“”«»]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ── catalogues ──────────────────────────────────────────────────────────────

SITES: Dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "correo": "https://mail.google.com",
    "calendario": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
    "agenda": "https://calendar.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "mapas": "https://maps.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "whatsapp web": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "tiktok": "https://www.tiktok.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv",
    "amazon": "https://www.amazon.es",
    "wikipedia": "https://es.wikipedia.org",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "github": "https://github.com",
    "shopify": "https://admin.shopify.com",
    "notion": "https://www.notion.so",
    "canva": "https://www.canva.com",
    "spotify web": "https://open.spotify.com",
    "traductor": "https://translate.google.com",
}

# Windows programs reachable without searching the Start Menu.
WINDOWS_APPS: Dict[str, str] = {
    "bloc de notas": "notepad.exe",
    "notepad": "notepad.exe",
    "calculadora": "calc.exe",
    "explorador": "explorer.exe",
    "explorador de archivos": "explorer.exe",
    "archivos": "explorer.exe",
    "paint": "mspaint.exe",
    "configuracion": "ms-settings:",
    "ajustes": "ms-settings:",
    "administrador de tareas": "taskmgr.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "app de camara": "microsoft.windows.camera:",
    "reloj": "ms-clock:",
    "alarmas": "ms-clock:",
    "calendario de windows": "outlookcal:",
    "correo de windows": "outlookmail:",
    "tienda": "ms-windows-store:",
    "spotify": "spotify:",
    "whatsapp app": "whatsapp:",
    "recortes": "ms-screenclip:",
}

FOLDERS: Dict[str, str] = {
    "descargas": "Downloads",
    "documentos": "Documents",
    "escritorio": "Desktop",
    "imagenes": "Pictures",
    "fotos": "Pictures",
    "musica": "Music",
    "videos": "Videos",
}

# Windows virtual-key codes for media keys.
VK = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "prev": 0xB1,
    "mute": 0xAD,
    "vol_down": 0xAE,
    "vol_up": 0xAF,
}


# ── command parsing ─────────────────────────────────────────────────────────


@dataclass
class Action:
    kind: str  # url | youtube | search | app | folder | media
    arg: str = ""
    label: str = ""


_OPEN = r"(?:abre(?:me)?|abrir|ve a|vete a|lanza|inicia|ejecuta|entra en|llevame a)"
_PLAY = r"(?:pon(?:me)?|reproduce|busca(?:me)?|quiero ver|ensename)"


def parse_command(text: str) -> Optional[Action]:
    """Recognise a PC command in Spanish, or return None."""
    t = _fold(text)
    t = re.sub(r"^(?:oye |hey |eh )?jarvis\s*", "", t)
    t = re.sub(r"^(?:por favor |porfa |puedes |podrias )", "", t)
    t = re.sub(r"\s+por favor$", "", t)

    # Media keys
    media = [
        ("pausa|para la musica|deten la musica|pausa la musica", "play_pause", "Pausa"),
        ("reanuda|continua la musica|dale al play|play", "play_pause", "Reanudo"),
        (
            "siguiente cancion|siguiente tema|pasa de cancion|salta la cancion",
            "next",
            "Siguiente",
        ),
        ("cancion anterior|tema anterior|vuelve a la anterior", "prev", "Anterior"),
        ("silencia|quita el sonido|mutea", "mute", "Silencio"),
        ("sube el volumen|mas volumen|sube volumen", "vol_up", "Subo el volumen"),
        ("baja el volumen|menos volumen|baja volumen", "vol_down", "Bajo el volumen"),
    ]
    for words, key, label in media:
        if re.search(rf"\b({words})\b", t) and len(t) < 40:
            return Action("media", key, label)

    # YouTube: "pon el último vídeo de X", "busca en youtube X", "pon X en youtube"
    m = re.search(
        rf"^{_PLAY}\s+(?:en youtube\s+)?(?:un |el |la |los |las )?"
        r"(?:ultimo |nuevo |primer )?(?:video|videos|cancion|tema|directo)?\s*"
        r"(?:de |del |sobre )?(.+?)(?:\s+en youtube)?$",
        t,
    )
    if m and (
        "youtube" in t or re.search(r"\b(video|videos|cancion|tema|directo)\b", t)
    ):
        query = m.group(1).strip()
        if query and query != "youtube":
            if "ultimo" in t or "nuevo" in t:
                query += " último vídeo"
            return Action("youtube", query, f"Pongo {m.group(1).strip()} en YouTube")
    m = re.search(rf"^{_OPEN}\s+youtube\s+(?:y\s+)?(?:pon|busca)\s+(.+)$", t)
    if m:
        return Action(
            "youtube", m.group(1).strip(), f"Pongo {m.group(1).strip()} en YouTube"
        )

    # Web search
    m = re.search(
        r"^(?:busca|buscame|googlea)\s+(?:en google\s+|en internet\s+)?(.+)$", t
    )
    if m and "youtube" not in t:
        return Action("search", m.group(1).strip(), f"Busco {m.group(1).strip()}")

    # Maps: "cómo llego a X", "ruta a X"
    m = re.search(r"^(?:como llego a|ruta (?:a|hasta)|llevame a)\s+(.+)$", t)
    if m:
        dest = m.group(1).strip()
        return Action(
            "url",
            f"https://www.google.com/maps/dir/?api=1&destination={quote_plus(dest)}",
            f"Abro la ruta a {dest}",
        )

    # Open a web address said or typed literally ("abre amazon.es")
    raw = (text or "").lower()
    m = re.search(rf"^{_OPEN}\b", t) and re.search(
        r"\b((?:[\w-]+\.)+(?:com|es|org|net|io|ai|tv|app|dev)(?:/\S*)?)", raw
    )
    if m:
        return Action("url", "https://" + m.group(1), f"Abro {m.group(1)}")

    # Open …
    m = re.search(rf"^{_OPEN}\s+(?:el |la |los |las |mi |mis )?(.+)$", t)
    if m:
        target = m.group(1).strip()
        target = re.sub(
            r"^(?:pagina de |web de |app de |aplicacion de |programa )", "", target
        )
        if target in ("camara", "webcam", "camara web"):
            return Action("hud", "camera_on", "Activo la cámara")
        folder = re.sub(r"^carpeta (?:de )?", "", target)
        if folder in FOLDERS:
            return Action("folder", FOLDERS[folder], f"Abro {folder}")
        if target in SITES:
            return Action("url", SITES[target], f"Abro {target}")
        if target in WINDOWS_APPS:
            return Action("app", target, f"Abro {target}")
        return Action("app", target, f"Abro {target}")
    return None


# ── execution ───────────────────────────────────────────────────────────────


def youtube_first_video(query: str, timeout: float = 6.0) -> Optional[str]:
    """URL of the first YouTube result for *query* (no API key needed)."""
    try:
        import httpx

        resp = httpx.get(
            "https://www.youtube.com/results",
            params={"search_query": query, "hl": "es"},
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "es-ES,es;q=0.9"},
            timeout=timeout,
        )
        for vid in re.findall(r'"videoId":"([\w-]{11})"', resp.text):
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as exc:  # noqa: BLE001
        logger.info("Búsqueda en YouTube falló: %s", exc)
    return None


def _start_menu_dirs() -> List[Path]:
    dirs = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(
                Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            )
    return [d for d in dirs if d.exists()]


def find_installed_app(name: str) -> Optional[Path]:
    """Best Start Menu shortcut whose name matches *name* (accents ignored)."""
    want = _fold(name)
    if not want:
        return None
    best: Tuple[int, Optional[Path]] = (0, None)
    for root in _start_menu_dirs():
        for lnk in root.rglob("*.lnk"):
            stem = _fold(lnk.stem)
            if "uninstall" in stem or "desinstal" in stem:
                continue
            score = 0
            if stem == want:
                score = 100
            elif stem.startswith(want):
                score = 80
            elif want in stem:
                score = 60
            elif all(w in stem for w in want.split()):
                score = 40
            if score > best[0] or (
                score == best[0] and best[1] and len(lnk.stem) < len(best[1].stem)
            ):
                best = (score, lnk)
    return best[1]


def _launch(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", target])  # noqa: S603, S607
    else:
        import subprocess

        subprocess.Popen(["xdg-open", target])  # noqa: S603, S607


def _media_key(key: str, times: int = 1) -> None:
    if sys.platform != "win32":
        raise RuntimeError("El control multimedia solo está disponible en Windows.")
    import ctypes

    vk = VK[key]
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)  # type: ignore[attr-defined]
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # type: ignore[attr-defined]


def execute(action: Action, open_url: Callable[[str], object] = webbrowser.open) -> str:
    """Carry out *action*; return a short sentence for Jarvis to say."""
    if action.kind == "media":
        steps = 5 if action.arg in ("vol_up", "vol_down") else 1
        _media_key(action.arg, steps)
        return f"{action.label}."
    if action.kind == "url":
        open_url(action.arg)
        return f"{action.label}."
    if action.kind == "search":
        open_url(f"https://www.google.com/search?q={quote_plus(action.arg)}")
        return f"{action.label}."
    if action.kind == "youtube":
        url = youtube_first_video(action.arg)
        if url:
            open_url(url)
            return f"{action.label}."
        open_url(
            f"https://www.youtube.com/results?search_query={quote_plus(action.arg)}"
        )
        return f"Te dejo los resultados de {action.arg} en YouTube."
    if action.kind == "folder":
        path = Path.home() / action.arg
        _launch(str(path))
        return f"{action.label}."
    if action.kind == "app":
        name = action.arg
        if name in WINDOWS_APPS:
            _launch(WINDOWS_APPS[name])
            return f"{action.label}."
        lnk = find_installed_app(name)
        if lnk is not None:
            _launch(str(lnk))
            return f"Abro {lnk.stem}."
        # Maybe it's a website people call by name ("abre canva").
        site = SITES.get(name)
        if site:
            open_url(site)
            return f"{action.label}."
        raise LookupError(f"No encuentro ningún programa llamado «{name}».")
    raise ValueError(f"Acción desconocida: {action.kind}")


__all__ = [
    "Action",
    "FOLDERS",
    "SITES",
    "WINDOWS_APPS",
    "execute",
    "find_installed_app",
    "parse_command",
    "youtube_first_video",
]
