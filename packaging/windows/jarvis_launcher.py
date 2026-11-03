"""Jarvis.exe — double-click to open Jarvis (no terminal).

* If Jarvis is already running, it is brought to the front.
* Otherwise it starts Jarvis from the installed copy, without a console.

Only the standard library is used, so the .exe stays small. Build it with
``packaging/windows/build_exe.ps1``.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OpenJarvis" / "src"
CONFIG = Path.home() / ".openjarvis" / "jarvis.toml"


def _port() -> int:
    try:
        with open(CONFIG, "rb") as fh:
            data = tomllib.load(fh) if tomllib else {}
        return int((data.get("interfaz") or {}).get("puerto", 8765))
    except (OSError, ValueError, TypeError):
        return 8765


def _wake(port: int) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/wake",
        data=json.dumps({}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
            return resp.status == 200
    except OSError:
        return False


def _message(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "Jarvis", 0x40)


def main() -> int:
    background = "--segundo-plano" in sys.argv
    if not background and _wake(_port()):
        return 0
    pythonw = HOME / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        _message(
            "No encuentro la instalación de Jarvis en\n"
            f"{HOME}\n\nInstálalo primero (ver JARVIS.md)."
        )
        return 1
    args = [str(pythonw), "-m", "openjarvis.hud"]
    if background:
        args.append("--segundo-plano")
    subprocess.Popen(  # noqa: S603
        args,
        cwd=str(HOME),
        creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | NO_WINDOW
        close_fds=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
