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


def _browser_candidates(prefer: str = "auto") -> List[str]:
    """Installed Edge/Chrome executables; *prefer* ("chrome"/"edge") goes first."""
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
    want = (prefer or "auto").lower()
    if want in ("chrome", "edge"):
        key = "chrome" if want == "chrome" else "edge"
        found.sort(key=lambda exe: key not in Path(exe).name.lower())
    return found


def _grant_media(profile: Path, url: str) -> None:
    """Allow microphone and camera for the Jarvis page in its own profile.

    Saves a permission prompt on every start. Best effort: the browser
    rewrites this file while it runs, so it only applies on a fresh start.
    """
    import json

    origin = url.rstrip("/")
    prefs_path = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    exceptions = (
        prefs.setdefault("profile", {})
        .setdefault("content_settings", {})
        .setdefault("exceptions", {})
    )
    for kind in ("media_stream_mic", "media_stream_camera"):
        exceptions.setdefault(kind, {})[f"{origin},*"] = {"setting": 1}
    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError:
        pass


def open_window(url: str, *, fullscreen: bool = False, browser: str = "auto") -> bool:
    """Open *url* in a frameless Edge/Chrome app window (fallback: default browser).

    *browser* is ``[interfaz] navegador``: "chrome", "edge" or "auto" (Edge
    first, as it comes with Windows). Each browser gets its own small Jarvis
    profile, separate from the user's normal one.
    """
    for exe in _browser_candidates(browser):
        name = (
            "hud-window-chrome" if "chrome" in Path(exe).name.lower() else "hud-window"
        )
        profile = get_config_dir() / name
        profile.mkdir(parents=True, exist_ok=True)
        _grant_media(profile, url)
        args = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
            "--disable-features=Translate,CalculateNativeWinOcclusion",
            # Keep listening for "Jarvis, enciéndete" while hidden.
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
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
    """VBScript that starts Jarvis hidden in the background at log-on."""
    cmd = f'"{_pythonw()}" -m openjarvis.hud --segundo-plano'
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


API_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
)


def load_user_keys() -> List[str]:
    """Read API keys saved with ``setx`` that this process did not inherit.

    A program started from an old Explorer, a shortcut or another app may not
    see keys added later with ``setx``; Windows keeps them in the user's
    registry ("Environment" under HKEY_CURRENT_USER), so read them there.
    """
    if sys.platform != "win32":
        return []
    import winreg

    loaded = []
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except OSError:
        return []
    with key:
        for name in API_KEYS:
            if os.environ.get(name):
                continue
            try:
                value, _kind = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                os.environ[name] = value.strip()
                loaded.append(name)
    return loaded


def autostart_installed() -> bool:
    folder = _startup_dir()
    return bool(folder and (folder / STARTUP_NAME).exists())


# ── helpers for the app (no terminal for the user) ──────────────────────────

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_hidden(args: List[str], **kwargs: object) -> subprocess.CompletedProcess:
    """Run a command without flashing a console window."""
    return subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        encoding="utf-8",
        errors="replace",
        **kwargs,  # type: ignore[arg-type]
    )


def repo_root() -> Path:
    """The git checkout Jarvis runs from (…/OpenJarvis/src)."""
    import openjarvis

    return Path(openjarvis.__file__).resolve().parents[2]


def app_dir() -> Path:
    """Where Jarvis.exe and its icon live (…/OpenJarvis)."""
    return repo_root().parent


def _shortcut(lnk: Path, target: str, args: str, icon: Path, workdir: Path) -> None:
    def q(text: object) -> str:
        return str(text).replace("'", "''")

    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{q(lnk)}'); $s.TargetPath = '{q(target)}'; $s.Arguments = '{q(args)}'; "
        f"$s.IconLocation = '{q(icon)}'; $s.WorkingDirectory = '{q(workdir)}'; "
        "$s.Description = 'J.A.R.V.I.S.'; $s.Save()"
    )
    res = run_hidden(["powershell", "-NoProfile", "-Command", script], timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"No se pudo crear el acceso directo: {res.stderr[-300:]}")


def install_app() -> List[Path]:
    """Desktop + Start Menu shortcuts called «Jarvis», with its own icon.

    They open ``Jarvis.exe`` when it has been built, or Python directly
    otherwise — either way, no terminal for the user.
    """
    if sys.platform != "win32":
        raise RuntimeError("Los accesos directos solo están disponibles en Windows.")
    from openjarvis.hud.tray import save_ico

    folder = app_dir()
    icon = save_ico(folder / "jarvis.ico")
    exe = folder / "Jarvis.exe"
    if exe.exists():
        target, args = str(exe), ""
    else:
        target, args = _pythonw(), "-m openjarvis.hud"
    # The Desktop may live in OneDrive ("Escritorio"): ask Windows where it is.
    desk = run_hidden(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "[Environment]::GetFolderPath('Desktop')",
        ],
        timeout=20,
    ).stdout.strip()
    places = [
        Path(desk) if desk else Path.home() / "Desktop",
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs",
    ]
    made = []
    for place in places:
        if place.exists():
            lnk = place / "Jarvis.lnk"
            _shortcut(lnk, target, args, icon, repo_root())
            made.append(lnk)
    return made


def find_uv() -> Optional[str]:
    found = shutil.which("uv")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".cargo" / "bin" / "uv.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "uv.exe",
    ):
        if cand.exists():
            return str(cand)
    return None


SYNC_ARGS = [
    "sync",
    "--extra",
    "desktop",
    "--extra",
    "hud",
    "--extra",
    "inference-cloud",
    "--group",
    "desktop-native",
]


def schedule_update_and_restart() -> None:
    """Pull the latest code, then (once this process has exited) sync and restart.

    ``uv sync`` cannot replace files this Python process has open, so a small
    hidden PowerShell waits for us to exit before installing and relaunching.
    """
    root = repo_root()
    pull = run_hidden(["git", "pull", "--ff-only"], cwd=root, timeout=120)
    if pull.returncode != 0:
        raise RuntimeError(
            f"No se pudo descargar la actualización: {pull.stderr[-300:]}"
        )
    uv = find_uv() or "uv"
    sync = " ".join(f"'{a}'" for a in SYNC_ARGS)
    log = get_config_dir() / "jarvis-update.log"
    script = (
        f"Start-Sleep -Seconds 3; Set-Location -LiteralPath '{root}'; "
        f"& '{uv}' {sync} *> '{log}'; "
        f"Start-Process -FilePath '{_pythonw()}' "
        "-ArgumentList '-m','openjarvis.hud'"
    )
    subprocess.Popen(  # noqa: S603
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        creationflags=_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def local_addresses() -> List[str]:
    """This PC's addresses on the local network (for the Chromebook)."""
    import socket

    ips: List[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 80))  # picks the LAN interface; sends nothing
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


__all__ = [
    "autostart_installed",
    "find_uv",
    "install_autostart",
    "load_user_keys",
    "local_addresses",
    "open_window",
    "remove_autostart",
    "repo_root",
    "run_hidden",
    "schedule_update_and_restart",
    "startup_script",
]
