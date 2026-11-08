"""Free disk space and check the PC's health — directly, without mouse clicks.

* :func:`analyze` measures what can safely go: temporary files, the Recycle
  Bin, browser caches and old installers left in Downloads, and lists your
  biggest downloads (never deleted automatically).
* :func:`clean` removes only the categories the user confirmed. Old
  installers go to the Recycle Bin, so they can be recovered.
* :func:`pc_status` and :func:`close_program` report what is using memory
  and CPU, and close a program on request.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)

CATEGORIES = {
    "temporales": "archivos temporales",
    "papelera": "la papelera",
    "cache_navegadores": "la caché de los navegadores",
    "instaladores_viejos": "instaladores viejos de Descargas (a la papelera)",
}
_INSTALLERS = {".exe", ".msi", ".zip", ".rar", ".7z", ".iso", ".msix"}
_PROTECTED = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "smss.exe", "explorer.exe",
    "dwm.exe", "fontdrvhost.exe", "registry", "memory compression",
    "memcompression", "idle",
}  # fmt: skip


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}".replace(".0 ", " ").replace(".", ",")
        size /= 1024
    return f"{size} B"


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def temp_dirs() -> List[Path]:
    dirs = [Path(os.environ.get("TEMP", _local() / "Temp"))]
    win_temp = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Temp"
    if win_temp.exists():
        dirs.append(win_temp)
    return [d for d in dirs if d.exists()]


def cache_dirs() -> List[Path]:
    """Browser cache folders (safe to delete: they are rebuilt automatically)."""
    out: List[Path] = []
    roots = [
        _local() / "Google" / "Chrome" / "User Data",
        _local() / "Microsoft" / "Edge" / "User Data",
        _local() / "BraveSoftware" / "Brave-Browser" / "User Data",
        Path(os.environ.get("APPDATA", "")) / "Opera Software" / "Opera Stable",
    ]
    for root in roots:
        if not root.exists():
            continue
        for profile in [root, *[p for p in root.iterdir() if p.is_dir()]]:
            for name in ("Cache", "Code Cache", "GPUCache"):
                path = profile / name
                if path.is_dir():
                    out.append(path)
    return out


def downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _walk(
    paths: Iterable[Path], limit: int = 300_000
) -> Iterable[Tuple[Path, os.stat_result]]:
    count = 0
    stack = [str(p) for p in paths]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    count += 1
                    if count > limit:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path), entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
        except OSError:
            continue


def _size(paths: Iterable[Path]) -> int:
    return sum(st.st_size for _, st in _walk(paths))


def recycle_bin_size() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                    ("i64NumItems", ctypes.c_longlong)]  # fmt: skip

    info = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))  # type: ignore[attr-defined]
    return int(info.i64Size)


def old_installers(days: int = 30) -> List[Tuple[Path, int]]:
    cutoff = time.time() - days * 86400
    out = []
    for path, st in _walk([downloads_dir()], limit=50_000):
        if path.suffix.lower() in _INSTALLERS and st.st_mtime < cutoff:
            out.append((path, st.st_size))
    return out


def analyze() -> Dict[str, object]:
    """Measure what can be freed; nothing is deleted here."""
    installers = old_installers()
    big = sorted(
        ((p, st.st_size) for p, st in _walk([downloads_dir()], limit=50_000)),
        key=lambda x: x[1],
        reverse=True,
    )[:8]
    usage = {}
    for drive in ("C:\\", "D:\\"):
        if Path(drive).exists():
            import shutil

            total, _used, free = shutil.disk_usage(drive)
            usage[drive[0]] = {"libre": free, "total": total}
    return {
        "temporales": _size(temp_dirs()),
        "papelera": recycle_bin_size(),
        "cache_navegadores": _size(cache_dirs()),
        "instaladores_viejos": sum(s for _, s in installers),
        "n_instaladores": len(installers),
        "descargas": _size([downloads_dir()]),
        "mas_grandes": [(p.name, s) for p, s in big],
        "discos": usage,
    }


def describe(report: Dict[str, object]) -> str:
    parts = [f"{CATEGORIES[k]}: {human(report[k])}" for k in CATEGORIES]  # type: ignore[arg-type]
    total = sum(int(report[k]) for k in CATEGORIES)  # type: ignore[call-overload]
    discos = ", ".join(
        f"disco {d}: {human(v['libre'])} libres de {human(v['total'])}"
        for d, v in report["discos"].items()  # type: ignore[union-attr]
    )
    grandes = "; ".join(f"{n} ({human(s)})" for n, s in report["mas_grandes"])  # type: ignore[union-attr]
    return (
        f"Se pueden liberar {human(total)} en total: " + "; ".join(parts) + ". "
        f"Descargas ocupa {human(report['descargas'])} "  # type: ignore[arg-type]
        f"({report['n_instaladores']} instaladores de hace más de 30 días). "
        f"Archivos más grandes en Descargas: {grandes or 'ninguno'}. {discos}."
    )


def _delete_files(paths: Iterable[Path], older_than: float = 0) -> int:
    freed = 0
    cutoff = time.time() - older_than
    for path, st in list(_walk(paths)):
        if older_than and st.st_mtime > cutoff:
            continue
        try:
            path.unlink()
            freed += st.st_size
        except OSError:
            continue  # in use: skip it
    return freed


def _to_recycle_bin(files: List[Path]) -> int:
    if sys.platform != "win32" or not files:
        return 0
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]  # fmt: skip

    sizes = sum(f.stat().st_size for f in files if f.exists())
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = "\0".join(str(f) for f in files) + "\0\0"
    op.fFlags = (
        0x0040 | 0x0010 | 0x0004 | 0x0400
    )  # ALLOWUNDO|NOCONFIRM|SILENT|NOERRORUI
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
    return sizes


def clean(categories: Iterable[str]) -> Dict[str, int]:
    """Delete the confirmed categories; returns bytes freed per category."""
    done: Dict[str, int] = {}
    for cat in categories:
        if cat == "temporales":
            done[cat] = _delete_files(temp_dirs(), older_than=86400)  # keep today's
        elif cat == "cache_navegadores":
            done[cat] = _delete_files(cache_dirs())
        elif cat == "papelera" and sys.platform == "win32":
            import ctypes

            size = recycle_bin_size()
            ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)  # type: ignore[attr-defined]
            done[cat] = size
        elif cat == "instaladores_viejos":
            done[cat] = _to_recycle_bin([p for p, _ in old_installers()])
    return done


def pc_status() -> str:
    """What is using the PC right now, in plain words."""
    import psutil

    psutil.cpu_percent(None)
    procs = list(psutil.process_iter(["name", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(1.0)
    rows: Dict[str, List[float]] = {}
    for p in procs:
        try:
            name = (p.info["name"] or "?").lower()
            mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
            cpu = p.cpu_percent(None) / psutil.cpu_count()
        except psutil.Error:
            continue
        if name in _PROTECTED:
            continue  # Windows itself, not something the user can close
        acc = rows.setdefault(name, [0.0, 0.0])
        acc[0] += mem
        acc[1] += cpu
    top_mem = sorted(rows.items(), key=lambda kv: kv[1][0], reverse=True)[:6]
    top_cpu = sorted(rows.items(), key=lambda kv: kv[1][1], reverse=True)[:4]
    vm = psutil.virtual_memory()
    return (
        f"Procesador al {psutil.cpu_percent(None):.0f} %, memoria al "
        f"{vm.percent:.0f} % ({human(vm.available)} libres). "
        "Lo que más memoria usa: "
        + ", ".join(f"{n.removesuffix('.exe')} {human(m)}" for n, (m, _) in top_mem)
        + ". Lo que más procesador usa: "
        + ", ".join(f"{n.removesuffix('.exe')} {c:.0f} %" for n, (_, c) in top_cpu)
        + "."
    )


def matching_processes(name: str) -> List[object]:
    import psutil

    want = name.lower().removesuffix(".exe").strip()
    found = []
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info["name"] or "").lower()
        if pname in _PROTECTED or p.info["pid"] == os.getpid():
            continue
        if want and want in pname.removesuffix(".exe"):
            found.append(p)
    return found


def close_program(name: str) -> str:
    import psutil

    procs = matching_processes(name)
    if not procs:
        return f"No encuentro ningún programa abierto llamado «{name}»."
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=4)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return f"Cerrado {name} ({len(procs)} procesos)."


__all__ = [
    "CATEGORIES",
    "analyze",
    "clean",
    "close_program",
    "describe",
    "human",
    "matching_processes",
    "pc_status",
]
