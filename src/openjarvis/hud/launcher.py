"""Open the HUD as an app window and manage Windows auto-start."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from openjarvis.core.paths import get_config_dir

STARTUP_NAME = "Jarvis.vbs"


def _browser_candidates() -> List[str]:
    found: List[str] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
        ]
        for rel in rels:
            for root in roots:
                if root and Path(root, rel).exists():
                    found.append(str(Path(root, rel)))
    elif sys.platform == "darwin":
        for app in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if Path(app).exists():
                found.append(app)
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


def open_window(url: str, *, fullscreen: bool = False) -> bool:
    """Open *url* in a frameless Edge/Chrome app window (fallback: default browser)."""
    profile = get_config_dir() / "hud-window"
    profile.mkdir(parents=True, exist_ok=True)
    for exe in _browser_candidates():
        args = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--autoplay-policy=no-user-gesture-required",
            # Dedicated Jarvis profile: allow its own camera preview without a
            # permission prompt (the page only runs on 127.0.0.1).
            "--use-fake-ui-for-media-stream",
            "--no-first-run",
            "--disable-features=Translate",
            "--window-size=1600,900",
        ]
        if fullscreen:
            args.append("--start-fullscreen")
        try:
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                if sys.platform == "win32"
                else 0
            )
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        except OSError:
            continue
    return webbrowser.open(url)


# ── Windows auto-start ──────────────────────────────────────────────────────


def _startup_dir() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return (
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def startup_script() -> str:
    """VBScript that starts ``jarvis hud`` silently (no console window)."""
    cmd = f'"{_pythonw()}" -m openjarvis.cli --quiet hud'
    escaped = cmd.replace('"', '""')
    return (
        "' Arranca Jarvis al iniciar Windows (jarvis hud --autoarranque)\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{escaped}", 0, False\r\n'
    )


def install_autostart() -> Path:
    folder = _startup_dir()
    if folder is None:
        raise RuntimeError("El arranque automático solo está disponible en Windows.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / STARTUP_NAME
    # VBScript reads files in the ANSI code page, not UTF-8.
    target.write_text(
        startup_script(), encoding="mbcs" if sys.platform == "win32" else "utf-8"
    )
    return target


def remove_autostart() -> bool:
    folder = _startup_dir()
    if folder is None:
        return False
    target = folder / STARTUP_NAME
    if target.exists():
        target.unlink()
        return True
    return False


__all__ = ["install_autostart", "open_window", "remove_autostart", "startup_script"]
