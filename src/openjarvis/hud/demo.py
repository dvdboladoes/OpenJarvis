"""Scripted demo of the HUD — no microphone, model or accounts needed.

``jarvis hud --demo`` shows sample data and plays a fake wake → briefing →
conversation cycle so the interface can be previewed (and screenshotted).
"""

from __future__ import annotations

import math
import random
import threading
import time
import uuid
from datetime import datetime

from openjarvis.hud.config import HudConfig
from openjarvis.hud.server import EventHub, HudServer

SAMPLE = {
    "agenda": [
        "10:00 Llamada con proveedor de packaging",
        "12:30 Comida con Marta (socia)",
        "16:00 Grabación de reels para Instagram",
        "18:30 Revisión semanal de ventas",
        "20:00 Gimnasio",
    ],
    "mensajes": [
        "Lucía Pérez — Presupuesto para 40 unidades (sin responder)",
        "Shopify — Nuevo pedido #1043 por 89,90 €",
        "Tarea: enviar factura a Distribuciones Norte",
    ],
    "tareas": [],
    "otros": [],
    "tiempo": "Parcialmente nublado, +14°C",
    "metricas_titulo": "Negocio",
    "metricas": [
        {"nombre": "Leads hoy", "valor": "23", "cambio": "+18%", "tendencia": "up"},
        {
            "nombre": "Ventas ayer",
            "valor": "1.240 €",
            "cambio": "+6%",
            "tendencia": "up",
        },
        {
            "nombre": "Conversión",
            "valor": "3,4%",
            "cambio": "-0,3",
            "tendencia": "down",
        },
        {
            "nombre": "Seguidores",
            "valor": "12.800",
            "cambio": "+212",
            "tendencia": "up",
        },
    ],
}

# (role, mood, text)
SCRIPT = [
    ("user", "", "¡Bon fucking giorno, Jarvis!"),
    (
        "jarvis",
        "bromista",
        "¡Buon giorno, señor! Vaya energía traemos hoy, me encanta. "
        "Cielo algo nublado y catorce grados, nada que nos frene.",
    ),
    (
        "jarvis",
        "entusiasta",
        "Y ojo, que han entrado veintitrés leads, un dieciocho por ciento más que "
        "ayer. Lucía Pérez sigue esperando su presupuesto. ¿Por dónde empezamos?",
    ),
    ("user", "", "Estoy pensando en subir los precios un diez por ciento."),
    (
        "jarvis",
        "serio",
        "Es una decisión seria, así que vamos con calma. Con la conversión "
        "bajando un poco, yo no lo haría a ciegas: probaría primero con los "
        "nuevos clientes durante dos semanas y miraría si los leads aguantan.",
    ),
]

VOICES = [
    {"motor": "edge", "voz": v, "nombre": n}
    for v, n in (
        ("es-ES-AlvaroNeural", "Álvaro · España"),
        ("en-US-AndrewMultilingualNeural", "Andrew · multilingüe, muy natural"),
        ("en-US-AvaMultilingualNeural", "Ava · multilingüe, expresiva"),
        ("es-ES-ElviraNeural", "Elvira · España"),
    )
]  # fmt: skip


class DemoRunner:
    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self.hub = EventHub()
        self._busy = threading.Lock()
        self._timers: list = []
        self._notes = [
            {"id": "n1", "texto": "Llamar a Lucía con el presupuesto", "creada": 0},
            {
                "id": "n2",
                "texto": "Probar subida de precios con clientes nuevos",
                "creada": 0,
            },
        ]
        self.server = HudServer(
            self.hub,
            {
                "activate": lambda _p: {"started": self.wake()},
                "listen": lambda _p: {"started": self.wake()},
                "ask": lambda p: {"started": self.wake(str(p.get("text", "")))},
                "stop": lambda _p: {"stopped": True},
                "refresh": lambda _p: {"refreshing": True},
                "voice": lambda p: {"ok": True},
                "timer": self._timer,
                "timer_cancel": self._timer_cancel,
                "note": self._note,
                "note_delete": self._note_delete,
                "mic": lambda p: (
                    self.hub.publish("mic", on=bool(p.get("on", True))) or {}
                ),
                "forget": lambda _p: {"ok": True},
            },
            port=cfg.puerto,
        )

    def start(self, auto: bool = True) -> None:
        self.server.start()
        self.hub.publish(
            "snapshot", generado=datetime.now().isoformat(timespec="seconds"), **SAMPLE
        )
        self.hub.publish(
            "status",
            activacion=["palmadas", "voz"],
            oido="Whisper medium (CPU)",
            voz="edge",
            motor="claude-sonnet-5-5 · cloud",
        )
        self.hub.publish("voices", items=VOICES, actual=VOICES[1])
        self.hub.publish("notes", items=self._notes)
        self._timer({"segundos": 1500, "etiqueta": "Pomodoro"})
        self._timers[0]["ends_at"] -= 640
        self.hub.publish("timers", items=self._timers)
        self.hub.publish(
            "state",
            state="reposo",
            label="Dos palmadas, «Hey Jarvis» o espacio para hablar",
        )
        threading.Thread(target=self._idle, daemon=True).start()
        if auto:
            threading.Thread(target=self._auto, daemon=True).start()

    # demo widgets
    def _timer(self, p: dict) -> dict:
        secs = int(p.get("segundos", 60))
        t = {
            "id": uuid.uuid4().hex[:6],
            "seconds": secs,
            "label": p.get("etiqueta") or f"{secs // 60} min",
            "ends_at": time.time() + secs,
        }
        self._timers.append(t)
        self.hub.publish("timers", items=self._timers)
        return t

    def _timer_cancel(self, p: dict) -> dict:
        self._timers = [t for t in self._timers if t["id"] != p.get("id")]
        self.hub.publish("timers", items=self._timers)
        return {"ok": True}

    def _note(self, p: dict) -> dict:
        n = {"id": uuid.uuid4().hex[:6], "texto": str(p.get("texto", "")), "creada": 0}
        self._notes.insert(0, n)
        self.hub.publish("notes", items=self._notes)
        return n

    def _note_delete(self, p: dict) -> dict:
        self._notes = [n for n in self._notes if n["id"] != p.get("id")]
        self.hub.publish("notes", items=self._notes)
        return {"ok": True}

    def _auto(self) -> None:
        time.sleep(3)
        while True:
            self.wake()
            time.sleep(45)

    def _idle(self) -> None:
        cpu, ram = 18.0, 54.0
        n = 0
        while True:
            self.hub.publish(
                "level", source="mic", value=round(random.random() * 0.12, 3)
            )
            n += 1
            if n % 16 == 0:
                cpu = max(4, min(96, cpu + random.uniform(-6, 7)))
                ram = max(30, min(90, ram + random.uniform(-1, 1)))
                self.hub.publish(
                    "system", cpu=round(cpu), ram=round(ram), bateria=82, cargando=True
                )
            time.sleep(0.12)

    def _speak(self, text: str, mood: str, said: list) -> None:
        self.hub.publish("mood", mood=mood)
        self.hub.publish("state", state="hablando", label="")
        said.append(text)
        self.hub.publish("partial", role="jarvis", text=" ".join(said))
        dur = max(2.5, len(text) / 15)
        t0 = time.monotonic()
        while (t := time.monotonic() - t0) < dur:
            v = abs(math.sin(t * 7.3)) * 0.6 + random.random() * 0.4
            self.hub.publish("level", source="voz", value=round(v, 3))
            time.sleep(0.06)

    def wake(self, typed: str = "") -> bool:
        if not self._busy.acquire(blocking=False):
            return False

        def _run() -> None:
            try:
                if typed:
                    self.hub.publish("message", role="user", text=typed)
                    self.hub.publish("state", state="pensando", label="Procesando…")
                    time.sleep(1.2)
                    said: list = []
                    self._speak(
                        "Esto es una demostración, señor. Arránqueme sin --demo "
                        "para hablar de verdad.",
                        "alegre",
                        said,
                    )
                    self.hub.publish("message", role="jarvis", text=" ".join(said))
                    return
                self.hub.publish("wake", source="palmadas")
                self.hub.publish("state", state="activando", label="Sistemas en línea")
                time.sleep(1.0)
                said = []
                for role, mood, text in SCRIPT:
                    if role == "user":
                        if said:
                            self.hub.publish(
                                "message", role="jarvis", text=" ".join(said)
                            )
                            said = []
                        self.hub.publish(
                            "state", state="escuchando", label="Le escucho…"
                        )
                        t0 = time.monotonic()
                        while time.monotonic() - t0 < 2.0:
                            self.hub.publish(
                                "level",
                                source="mic",
                                value=round(0.3 + random.random() * 0.6, 3),
                            )
                            time.sleep(0.08)
                        self.hub.publish("message", role="user", text=text)
                        self.hub.publish("state", state="pensando", label="Procesando…")
                        time.sleep(1.4)
                    else:
                        self._speak(text, mood, said)
                if said:
                    self.hub.publish("message", role="jarvis", text=" ".join(said))
            finally:
                self.hub.publish("mood", mood="neutral")
                self.hub.publish(
                    "state",
                    state="reposo",
                    label="Dos palmadas, «Hey Jarvis» o espacio para hablar",
                )
                self._busy.release()

        threading.Thread(target=_run, daemon=True).start()
        return True

    def stop(self) -> None:
        self.server.stop()


__all__ = ["DemoRunner"]
