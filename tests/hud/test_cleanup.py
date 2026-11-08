"""Disk cleanup and computer-use coordinates."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from openjarvis.hud import cleanup
from openjarvis.hud.agent import AWAIT_REPLY, ClaudeAgent
from openjarvis.hud.config import HudConfig
from openjarvis.hud.memory import MemoryStore


def _file(path: Path, size: int, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


def test_clean_temp_keeps_todays_files(tmp_path: Path, monkeypatch) -> None:
    temp = tmp_path / "Temp"
    old = _file(temp / "viejo.tmp", 1000, 3)
    new = _file(temp / "sub" / "hoy.tmp", 500, 0)
    monkeypatch.setattr(cleanup, "temp_dirs", lambda: [temp])
    assert cleanup.analyze.__doc__  # sanity
    freed = cleanup.clean(["temporales"])
    assert freed == {"temporales": 1000}
    assert not old.exists() and new.exists()


def test_clean_needs_a_spoken_yes(tmp_path: Path, monkeypatch) -> None:
    temp = tmp_path / "Temp"
    victim = _file(temp / "viejo.tmp", 2048, 5)
    monkeypatch.setattr(cleanup, "temp_dirs", lambda: [temp])
    monkeypatch.setattr(
        cleanup,
        "analyze",
        lambda: {
            "temporales": 2048,
            "papelera": 0,
            "cache_navegadores": 0,
            "instaladores_viejos": 0,
        },
    )
    brain = SimpleNamespace(_history=[], MAX_HISTORY=12, _facts="", _notes="")

    def run(answer: str) -> list:
        agent = ClaudeAgent(
            HudConfig(), brain, MemoryStore(None), get_reply=lambda _t: answer
        )
        gen = agent._clean(["temporales"])  # noqa: SLF001
        out = []
        try:
            while True:
                out.append(next(gen))
        except StopIteration as stop:
            out.append(stop.value)
        return out

    said = run("no")
    assert AWAIT_REPLY in said and "no se ha borrado nada" in said[-1]
    assert victim.exists()
    said = run("sí, adelante")
    assert said[-1].startswith("Liberados 2 KB")
    assert not victim.exists()


def test_installers_go_to_recycle_bin_only_when_old(
    tmp_path: Path, monkeypatch
) -> None:
    downloads = tmp_path / "Downloads"
    _file(downloads / "setup-viejo.exe", 100, 60)
    _file(downloads / "setup-nuevo.exe", 100, 2)
    _file(downloads / "foto.png", 100, 90)
    monkeypatch.setattr(cleanup, "downloads_dir", lambda: downloads)
    assert [p.name for p, _ in cleanup.old_installers()] == ["setup-viejo.exe"]


def test_protected_processes_are_never_matched() -> None:
    assert all(
        (p.info["name"] or "").lower() not in {"explorer.exe", "svchost.exe"}
        for p in cleanup.matching_processes("explorer")
        + cleanup.matching_processes("svchost")
    )


def test_human_sizes() -> None:
    assert cleanup.human(512) == "512 B"
    assert cleanup.human(2048) == "2 KB"
    assert cleanup.human(4.1 * 1024**3) == "4,1 GB"


def test_clicks_map_to_mouse_pixels_on_scaled_screens(monkeypatch) -> None:
    import pyautogui

    from openjarvis.hud.computer import Computer

    comp = Computer.__new__(Computer)
    comp.scale = 1.0
    comp.screen_size = (1920, 1080)  # physical capture
    monkeypatch.setattr(pyautogui, "size", lambda: (1536, 864))  # 125 % scaling
    assert comp._xy((960, 540)) == (768, 432)  # noqa: SLF001
    monkeypatch.setattr(pyautogui, "size", lambda: (1920, 1080))
    assert comp._xy((960, 540)) == (960, 540)  # noqa: SLF001
