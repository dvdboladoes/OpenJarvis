"""Instant skills handled without the language model: timers and notes.

These are deterministic so they work instantly even with a slow local model,
and the HUD can show them as live widgets.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

_NUM_WORDS = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
    "doce": 12, "quince": 15, "veinte": 20, "veinticinco": 25, "treinta": 30,
    "cuarenta": 40, "cuarenta y cinco": 45, "media": 30, "noventa": 90,
}  # fmt: skip
_UNIT_SECONDS = {"segundo": 1, "minuto": 60, "hora": 3600}

_TIMER_RE = re.compile(
    r"(temporizador|alarma|cuenta atr[aá]s|av[ií]same|cron[oó]metro)"
    r".*?\b(\d+|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\s*"
    r"(segundos?|minutos?|horas?)",
    re.I,
)
_NOTE_RE = re.compile(
    r"^\s*(?:jarvis[, ]+)?(?:apunta|anota|toma nota de|toma nota|guarda(?: la nota)?"
    r"|recu[eé]rdame)(?:\s+que)?[:,]?\s+(.{3,})$",
    re.I,
)


def parse_timer(text: str) -> Optional[tuple[int, str]]:
    """Return ``(seconds, label)`` for "pon un temporizador de 5 minutos"."""
    m = _TIMER_RE.search(text or "")
    if not m:
        return None
    raw, unit = m.group(2).lower(), m.group(3).lower().rstrip("s")
    qty = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 0)
    if qty <= 0:
        return None
    seconds = qty * _UNIT_SECONDS[unit]
    if seconds > 24 * 3600:
        return None
    word = {"segundo": "segundo", "minuto": "minuto", "hora": "hora"}[unit]
    label = f"{qty} {word}{'s' if qty != 1 else ''}"
    return seconds, label


def parse_note(text: str) -> Optional[str]:
    m = _NOTE_RE.match(text or "")
    if not m:
        return None
    note = m.group(1).strip().rstrip(".")
    return note[:1].upper() + note[1:] if note else None


@dataclass
class Timer:
    seconds: int
    label: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    ends_at: float = 0.0  # unix time

    def remaining(self) -> float:
        return max(0.0, self.ends_at - time.time())


class TimerManager:
    """Countdowns that call ``on_done(timer)`` when they finish."""

    def __init__(self, on_change: Callable[[List[dict]], None],
                 on_done: Callable[[Timer], None]) -> None:  # fmt: skip
        self._timers: Dict[str, Timer] = {}
        self._lock = threading.Lock()
        self._on_change = on_change
        self._on_done = on_done
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="jarvis-timers", daemon=True).start()

    def add(self, seconds: int, label: str = "") -> Timer:
        t = Timer(seconds=int(seconds), label=label or f"{seconds} s")
        t.ends_at = time.time() + t.seconds
        with self._lock:
            self._timers[t.id] = t
        self._emit()
        return t

    def cancel(self, timer_id: str) -> bool:
        with self._lock:
            found = self._timers.pop(timer_id, None) is not None
        self._emit()
        return found

    def list(self) -> List[dict]:
        with self._lock:
            return [
                asdict(t)
                for t in sorted(self._timers.values(), key=lambda x: x.ends_at)
            ]

    def stop(self) -> None:
        self._stop.set()

    def _emit(self) -> None:
        self._on_change(self.list())

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            done: List[Timer] = []
            with self._lock:
                for t in list(self._timers.values()):
                    if t.remaining() <= 0:
                        done.append(self._timers.pop(t.id))
            for t in done:
                self._emit()
                self._on_done(t)


class NotesStore:
    """Notes the user dictates ("apunta que…"), persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self._notes: List[dict] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._notes = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._notes, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def add(self, text: str) -> dict:
        note = {
            "id": uuid.uuid4().hex[:8],
            "texto": text.strip(),
            "creada": time.time(),
        }
        with self._lock:
            self._notes.append(note)
            self._notes = self._notes[-50:]
            self._save()
        return note

    def delete(self, note_id: str) -> bool:
        with self._lock:
            before = len(self._notes)
            self._notes = [n for n in self._notes if n.get("id") != note_id]
            self._save()
            return len(self._notes) != before

    def list(self) -> List[dict]:
        with self._lock:
            return list(reversed(self._notes))

    def texts(self) -> List[str]:
        return [n["texto"] for n in self.list()]


__all__ = ["NotesStore", "Timer", "TimerManager", "parse_note", "parse_timer"]
