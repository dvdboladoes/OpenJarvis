"""Collect the data Jarvis talks about: agenda, inbox, weather and metrics.

Everything here is best-effort — a source that is not connected or fails is
skipped, never fatal — because the briefing must always start.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.hud.config import HudConfig

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^===\s*(.+?)\s*===\s*$")


@dataclass
class Metric:
    nombre: str
    valor: str
    cambio: str = ""

    @property
    def tendencia(self) -> str:
        c = self.cambio.strip()
        if c.startswith("+"):
            return "up"
        if c.startswith("-") or c.startswith("−"):
            return "down"
        return ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "nombre": self.nombre,
            "valor": self.valor,
            "cambio": self.cambio,
            "tendencia": self.tendencia,
        }


@dataclass
class Snapshot:
    """Everything known about the user's day at one moment."""

    generado: datetime = field(default_factory=datetime.now)
    secciones: Dict[str, List[str]] = field(default_factory=dict)
    tiempo: str = ""
    metricas: List[Metric] = field(default_factory=list)
    metricas_titulo: str = "Métricas"
    errores: List[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        """Plain-text facts for the language model."""
        parts = [f"FECHA Y HORA: {self.generado.strftime('%A %d/%m/%Y %H:%M')}"]
        if self.tiempo:
            parts.append(f"TIEMPO: {self.tiempo}")
        for name, lines in self.secciones.items():
            if lines:
                parts.append(f"{name}:\n" + "\n".join(f"- {line}" for line in lines))
        if self.metricas:
            parts.append(
                f"{self.metricas_titulo.upper()}:\n"
                + "\n".join(
                    f"- {m.nombre}: {m.valor}" + (f" ({m.cambio})" if m.cambio else "")
                    for m in self.metricas
                )
            )
        if len(parts) == 1:
            parts.append("(No hay fuentes de datos conectadas todavía.)")
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generado": self.generado.isoformat(timespec="seconds"),
            "agenda": self.secciones.get("CALENDAR", []),
            "mensajes": self.secciones.get("MESSAGES", []),
            "tareas": self.secciones.get("TASKS", []),
            "otros": [
                line
                for name, lines in self.secciones.items()
                if name not in {"CALENDAR", "MESSAGES", "TASKS"}
                for line in lines
            ],
            "tiempo": self.tiempo,
            "metricas": [m.to_dict() for m in self.metricas],
            "metricas_titulo": self.metricas_titulo,
        }


# ── Connectors (Gmail, Calendar, Tasks…) ────────────────────────────────────


def parse_digest_sections(text: str) -> Dict[str, List[str]]:
    """Split ``digest_collect`` output (``=== NAME ===`` blocks) into lines."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).upper()
            sections.setdefault(current, [])
            continue
        if current and current != "ERRORS":
            # Drop the "[source]" prefix the formatter adds; it is noise on screen.
            sections[current].append(re.sub(r"^\[[\w\s-]+\]\s*", "", line))
    return {k: v for k, v in sections.items() if v}


def collect_connectors(sources: List[str], hours_back: float) -> tuple[dict, list]:
    if not sources:
        return {}, []
    try:
        from openjarvis.tools.digest_collect import DigestCollectTool

        result = DigestCollectTool().execute(sources=sources, hours_back=hours_back)
    except Exception as exc:  # noqa: BLE001
        logger.info("Connector collection failed: %s", exc)
        return {}, [str(exc)]
    errors = list((result.metadata or {}).get("sources_failed", []))
    return parse_digest_sections(result.content or ""), errors


# ── Weather (wttr.in, free, no key) ─────────────────────────────────────────


def fetch_weather(city: str, timeout: float = 6.0) -> str:
    if not city.strip():
        return ""
    try:
        import httpx

        resp = httpx.get(
            f"https://wttr.in/{city.strip()}",
            params={"format": "%C, %t, sensación %f, humedad %h", "lang": "es"},
            timeout=timeout,
            headers={"User-Agent": "curl/8"},
        )
        text = resp.text.strip()
        if resp.status_code == 200 and text and "<" not in text and len(text) < 160:
            return text
    except Exception as exc:  # noqa: BLE001
        logger.info("Weather lookup failed: %s", exc)
    return ""


# ── Business metrics (CSV / Google Sheets / JSON) ───────────────────────────


def parse_metrics(text: str) -> List[Metric]:
    """Parse metrics from CSV (``nombre,valor,cambio``) or a JSON list/dict."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return []
    if text[0] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            return [Metric(str(k), str(v)) for k, v in data.items()]
        out = []
        for row in data:
            if isinstance(row, dict):
                vals = list(row.values())
                nombre = (
                    row.get("nombre") or row.get("name") or (vals[0] if vals else "")
                )
                valor = (
                    row.get("valor")
                    or row.get("value")
                    or (vals[1] if len(vals) > 1 else "")
                )
                cambio = row.get("cambio") or row.get("change") or ""
                out.append(Metric(str(nombre), str(valor), str(cambio)))
        return [m for m in out if m.nombre]

    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    has_header = bool(header) and header[0] in {
        "nombre",
        "name",
        "métrica",
        "metrica",
        "kpi",
    }
    body = rows[1:] if has_header else rows
    metrics = []
    for r in body:
        cells = [c.strip() for c in r] + ["", ""]
        if cells[0]:
            metrics.append(Metric(cells[0], cells[1], cells[2]))
    return metrics[:12]


def load_metrics(source: str, timeout: float = 8.0) -> List[Metric]:
    source = (source or "").strip()
    if not source:
        return []
    try:
        if source.startswith(("http://", "https://")):
            import httpx

            resp = httpx.get(source, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            text = resp.text
        else:
            text = Path(source).expanduser().read_text(encoding="utf-8")
        return parse_metrics(text)
    except Exception as exc:  # noqa: BLE001
        logger.info("Metrics load failed (%s): %s", source, exc)
        return []


def collect_snapshot(cfg: HudConfig) -> Snapshot:
    """Gather all sources for the briefing (each one optional)."""
    snap = Snapshot(metricas_titulo=cfg.metricas_titulo)
    snap.secciones, snap.errores = collect_connectors(cfg.fuentes, cfg.horas_atras)
    if cfg.incluir_tiempo:
        snap.tiempo = fetch_weather(cfg.ciudad)
    snap.metricas = load_metrics(cfg.metricas_fuente)
    return snap


__all__ = [
    "Metric",
    "Snapshot",
    "collect_snapshot",
    "fetch_weather",
    "load_metrics",
    "parse_digest_sections",
    "parse_metrics",
]
