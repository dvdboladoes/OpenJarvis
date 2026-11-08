"""Routines ("modo trabajo") and dated reminders ("mañana a las 9…").

Both are small JSON files in ``~/.openjarvis``:

* **Routines** are named lists of quick steps that reuse Jarvis' own tools
  (open a website or app, play something on YouTube, volume, dark mode,
  media keys, say a sentence). «Modo trabajo» can open VS Code and Gmail,
  lower the volume and put lofi music on, all at once.
* **Reminders** fire at a date and time. A background thread checks them
  every few seconds and Jarvis says them out loud — also in the background.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Steps a routine may contain → the agent tool that runs each one.
STEP_ACTIONS = {
    "abrir_web": "url",
    "abrir_app": "nombre",
    "poner_youtube": "busqueda",
    "buscar_google": "consulta",
    "volumen": "nivel",
    "brillo": "nivel",
    "modo_oscuro": "activar",
    "multimedia": "accion",
    "decir": "texto",
}


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text).lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


class _JsonList:
    """A list of dicts saved atomically to a JSON file."""

    def __init__(self, path: Optional[Path], key: str) -> None:
        self.path, self.key = path, key
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._items = [i for i in data.get(key, []) if isinstance(i, dict)]
            except (OSError, ValueError) as exc:
                logger.warning("No se pudo leer %s: %s", path, exc)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({self.key: self._items}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self._items]


class RoutineStore(_JsonList):
    def __init__(self, path: Optional[Path]) -> None:
        super().__init__(path, "rutinas")

    @staticmethod
    def clean_steps(steps: Any) -> List[Dict[str, Any]]:
        out = []
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            action = str(step.get("accion", ""))
            if action in STEP_ACTIONS and str(step.get("valor", "")).strip():
                out.append(
                    {"accion": action, "valor": str(step["valor"]).strip()[:300]}
                )
        return out[:15]

    def save(self, name: str, steps: Any, trigger: str = "") -> Dict[str, Any]:
        """Create or replace the routine called *name*."""
        name = " ".join(str(name).split())[:60]
        steps = self.clean_steps(steps)
        if not name or not steps:
            raise ValueError("Una rutina necesita nombre y al menos un paso válido.")
        with self._lock:
            self._items = [r for r in self._items if _fold(r["nombre"]) != _fold(name)]
            item = {
                "id": uuid.uuid4().hex[:8],
                "nombre": name,
                "frase": " ".join(str(trigger).split())[:80],
                "pasos": steps,
            }
            self._items.append(item)
            self._save()
            return dict(item)

    def delete(self, ident: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [
                r
                for r in self._items
                if r["id"] != ident and _fold(r["nombre"]) != _fold(ident)
            ]
            if len(self._items) != before:
                self._save()
                return True
            return False

    def find(self, text: str) -> Optional[Dict[str, Any]]:
        """The routine *text* refers to ("modo trabajo", "activa la rutina X")."""
        t = _fold(text)
        with self._lock:
            for r in self._items:
                names = [_fold(r["nombre"]), _fold(r.get("frase", ""))]
                if any(n and (t == n or n in t) for n in names):
                    return dict(r)
        return None

    def as_prompt(self) -> str:
        items = self.list()
        return ", ".join(f"«{r['nombre']}»" for r in items) if items else ""


class ReminderStore(_JsonList):
    """Reminders with an exact time; :meth:`due` returns and clears the ripe ones."""

    def __init__(self, path: Optional[Path]) -> None:
        super().__init__(path, "recordatorios")

    def add(self, when: str, text: str) -> Dict[str, Any]:
        moment = parse_when(when)
        if moment is None:
            raise ValueError("No entiendo la fecha y hora (usa AAAA-MM-DDTHH:MM).")
        if moment.timestamp() < time.time() - 60:
            raise ValueError("Esa hora ya ha pasado.")
        item = {
            "id": uuid.uuid4().hex[:8],
            "cuando": moment.isoformat(timespec="minutes"),
            "texto": " ".join(str(text).split())[:300],
        }
        with self._lock:
            self._items.append(item)
            self._items.sort(key=lambda r: r["cuando"])
            self._save()
        return dict(item)

    def delete(self, ident: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [r for r in self._items if r["id"] != ident]
            if len(self._items) != before:
                self._save()
                return True
            return False

    def due(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now()
        with self._lock:
            ripe = [r for r in self._items if parse_when(r["cuando"]) <= now]  # type: ignore[operator]
            if ripe:
                self._items = [r for r in self._items if r not in ripe]
                self._save()
            return ripe


def parse_when(value: str) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", ""))
    except ValueError:
        return None
    return moment.replace(tzinfo=None, second=0, microsecond=0)


def spoken_when(iso: str) -> str:
    """«mañana a las 9:00», «hoy a las 18:30», «el 12/11 a las 10:00»."""
    moment = parse_when(iso)
    if moment is None:
        return iso
    days = (moment.date() - datetime.now().date()).days
    day = {0: "hoy", 1: "mañana", 2: "pasado mañana"}.get(days, f"el {moment:%d/%m}")
    return f"{day} a las {moment:%H:%M}"


class ReminderClock:
    """Background thread that fires reminders when their time comes."""

    def __init__(
        self, store: ReminderStore, on_due: Callable[[Dict[str, Any]], None]
    ) -> None:
        self.store, self.on_due = store, on_due
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="jarvis-reminders", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(5.0):
            for item in self.store.due():
                try:
                    self.on_due(item)
                except Exception:  # noqa: BLE001
                    logger.exception("Recordatorio fallido")


__all__ = [
    "STEP_ACTIONS",
    "ReminderClock",
    "ReminderStore",
    "RoutineStore",
    "parse_when",
    "spoken_when",
]
