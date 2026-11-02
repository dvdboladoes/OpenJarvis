"""Scripted demo of the HUD — no microphone, model or accounts needed.

``jarvis hud --demo`` shows sample data and plays a fake wake → briefing →
conversation cycle so the interface can be previewed (and screenshotted).
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime

from openjarvis.hud.config import HudConfig
from openjarvis.hud.server import EventHub, HudServer

SAMPLE = {
    "agenda": [
        "10:00 Llamada con proveedor de packaging",
        "12:30 Comida con Marta (socia)",
        "16:00 Grabación de reels para Instagram",
        "18:30 Revisión semanal de ventas",
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
        {"nombre": "Seguidores", "valor": "12,8k", "cambio": "+212", "tendencia": "up"},
    ],
}

SCRIPT = [
    (
        "jarvis",
        "Buenas tardes, señor. Catorce grados y parcialmente nublado. Hoy tiene "
        "cuatro compromisos; el más importante, la revisión de ventas a las seis y "
        "media. Han entrado veintitrés leads, un dieciocho por ciento más que ayer, "
        "aunque la conversión ha bajado ligeramente. Lucía Pérez sigue esperando su "
        "presupuesto. ¿En qué puedo ayudarle?",
    ),
    ("user", "Recuérdame qué tengo después de comer."),
    (
        "jarvis",
        "A las cuatro tiene la grabación de reels y a las seis y media la revisión "
        "semanal de ventas. Le sugiero preparar las cifras antes de las seis.",
    ),
]


class DemoRunner:
    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self.hub = EventHub()
        self._busy = threading.Lock()
        self.server = HudServer(
            self.hub,
            {
                "activate": lambda _p: {"started": self.wake()},
                "ask": lambda p: {"started": self.wake(str(p.get("text", "")))},
                "stop": lambda _p: {"stopped": True},
                "refresh": lambda _p: {"refreshing": True},
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
            oido=True,
            voz="edge",
            motor="demo",
        )
        self.hub.publish(
            "state", state="reposo", label="Dos palmadas o «Hey Jarvis» para activarme"
        )
        threading.Thread(target=self._idle_mic, daemon=True).start()
        if auto:
            threading.Thread(target=self._auto, daemon=True).start()

    def _auto(self) -> None:
        time.sleep(3)
        while True:
            self.wake()
            time.sleep(30)

    def _idle_mic(self) -> None:
        while True:
            self.hub.publish(
                "level", source="mic", value=round(random.random() * 0.12, 3)
            )
            time.sleep(0.12)

    def _speak(self, text: str) -> None:
        self.hub.publish("message", role="jarvis", text=text)
        self.hub.publish("state", state="hablando", label="")
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
                    self._speak(
                        "Esto es una demostración, señor. "
                        "Arránqueme sin --demo para hablar de verdad."
                    )
                    return
                self.hub.publish("wake", source="palmadas")
                self.hub.publish("state", state="activando", label="Sistemas en línea")
                time.sleep(1.0)
                self.hub.publish(
                    "state", state="pensando", label="Preparando su briefing…"
                )
                time.sleep(1.6)
                for role, text in SCRIPT:
                    if role == "user":
                        self.hub.publish(
                            "state", state="escuchando", label="Le escucho…"
                        )
                        t0 = time.monotonic()
                        while time.monotonic() - t0 < 2.2:
                            self.hub.publish(
                                "level",
                                source="mic",
                                value=round(0.3 + random.random() * 0.6, 3),
                            )
                            time.sleep(0.08)
                        self.hub.publish("message", role="user", text=text)
                        self.hub.publish("state", state="pensando", label="Procesando…")
                        time.sleep(1.3)
                    else:
                        self._speak(text)
            finally:
                self.hub.publish(
                    "state",
                    state="reposo",
                    label="Dos palmadas o «Hey Jarvis» para activarme",
                )
                self._busy.release()

        threading.Thread(target=_run, daemon=True).start()
        return True

    def stop(self) -> None:
        self.server.stop()


__all__ = ["DemoRunner"]
