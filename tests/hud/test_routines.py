"""Routines, dated reminders and dictation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjarvis.hud.agent import ClaudeAgent
from openjarvis.hud.brain import Spoken
from openjarvis.hud.config import HudConfig
from openjarvis.hud.hotkey import dictation_text
from openjarvis.hud.memory import MemoryStore
from openjarvis.hud.routines import ReminderStore, RoutineStore, spoken_when


def test_routines_are_saved_found_and_cleaned(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "r.json")
    item = store.save(
        "Modo trabajo",
        [
            {"accion": "abrir_app", "valor": "Visual Studio Code"},
            {"accion": "poner_youtube", "valor": "lofi"},
            {"accion": "borrar_todo", "valor": "C:/"},  # not a valid step: dropped
        ],
        trigger="a trabajar",
    )
    assert [s["accion"] for s in item["pasos"]] == ["abrir_app", "poner_youtube"]
    again = RoutineStore(tmp_path / "r.json")
    assert again.find("Jarvis, modo trabajo")["id"] == item["id"]
    assert again.find("venga, a trabajar")["id"] == item["id"]
    assert again.find("modo fiesta") is None
    with pytest.raises(ValueError):
        again.save("vacía", [])
    assert again.delete("modo trabajo") and again.list() == []


def test_reminders_fire_once_when_due(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "rec.json")
    soon = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
    item = store.add(soon, "llamar a Ana")
    assert spoken_when(item["cuando"]).startswith(("hoy", "mañana"))
    assert store.due() == []
    later = datetime.now() + timedelta(minutes=5)
    assert [r["texto"] for r in store.due(later)] == ["llamar a Ana"]
    assert store.due(later) == []  # only once
    with pytest.raises(ValueError):
        store.add("2001-01-01T10:00", "pasado")
    with pytest.raises(ValueError):
        store.add("mañana", "sin formato")


def test_dictation_punctuation_and_stop() -> None:
    assert dictation_text("hola Ana coma qué tal") == "hola Ana, qué tal "
    assert dictation_text("nos vemos mañana punto y aparte") == "nos vemos mañana.\n"
    assert dictation_text("Fin del dictado") is None
    assert dictation_text("Jarvis, para de escribir.") is None


def test_routine_runs_its_steps(tmp_path: Path) -> None:
    opened: list = []
    store = RoutineStore(tmp_path / "r.json")
    routine = store.save(
        "modo cine",
        [
            {"accion": "abrir_web", "valor": "netflix.com"},
            {"accion": "decir", "valor": "Disfrute de la película."},
        ],
    )
    brain = SimpleNamespace(_history=[], MAX_HISTORY=12, _facts="", _notes="")
    agent = ClaudeAgent(
        HudConfig(), brain, MemoryStore(None), open_url=opened.append, routines=store
    )
    said = list(agent.run_routine(routine))
    assert opened == ["https://netflix.com"]
    assert said == [Spoken("Disfrute de la película.", "alegre")]
