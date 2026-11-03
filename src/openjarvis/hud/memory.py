"""Long-term memory: what Jarvis knows about you, kept between sessions.

Stored as a small JSON list in ``~/.openjarvis/jarvis_recuerdos.json``. Each
memory is one short fact in plain Spanish ("Su canción favorita es Bohemian
Rhapsody, de Queen"). Claude adds and removes them with its ``recordar`` /
``olvidar`` tools, and every fact travels in the system prompt, so "pon mi
canción favorita" works days later.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_MEMORIES = 300
MAX_CHARS = 400


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class MemoryStore:
    """Thread-safe list of facts about the user, persisted to disk."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: List[Dict[str, str]] = []
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("recuerdos", []) if isinstance(data, dict) else []
            self._items = [
                {
                    "id": str(i.get("id") or uuid.uuid4().hex[:10]),
                    "texto": str(i.get("texto", ""))[:MAX_CHARS],
                    "fecha": str(i.get("fecha", "")),
                }
                for i in items
                if isinstance(i, dict) and str(i.get("texto", "")).strip()
            ]
        except (OSError, ValueError) as exc:
            logger.warning("No se pudo leer la memoria: %s", exc)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"recuerdos": self._items}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("No se pudo guardar la memoria: %s", exc)

    def list(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(i) for i in self._items]

    def add(self, text: str) -> Dict[str, str]:
        """Remember *text*; an identical fact is not stored twice."""
        text = " ".join(str(text).split())[:MAX_CHARS]
        if not text:
            raise ValueError("Recuerdo vacío")
        with self._lock:
            for item in self._items:
                if _norm(item["texto"]) == _norm(text):
                    return dict(item)
            item = {
                "id": uuid.uuid4().hex[:10],
                "texto": text,
                "fecha": time.strftime("%Y-%m-%d"),
            }
            self._items.append(item)
            self._items = self._items[-MAX_MEMORIES:]
            self._save()
            return dict(item)

    def delete(self, ident: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != ident]
            changed = len(self._items) != before
            if changed:
                self._save()
            return changed

    def forget_matching(self, text: str) -> int:
        """Delete memories that contain *text* (case-insensitive)."""
        needle = _norm(text)
        if not needle:
            return 0
        with self._lock:
            keep = [i for i in self._items if needle not in _norm(i["texto"])]
            removed = len(self._items) - len(keep)
            if removed:
                self._items = keep
                self._save()
            return removed

    def update(self, ident: str, text: str) -> bool:
        text = " ".join(str(text).split())[:MAX_CHARS]
        with self._lock:
            for item in self._items:
                if item["id"] == ident and text:
                    item["texto"] = text
                    self._save()
                    return True
            return False

    def as_prompt(self) -> str:
        with self._lock:
            if not self._items:
                return ""
            return "\n".join(f"- {i['texto']}" for i in self._items)


__all__ = ["MemoryStore"]
