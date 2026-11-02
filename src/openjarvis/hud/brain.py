"""Jarvis' personality and conversation memory (Spanish)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from openjarvis.hud.config import HudConfig

if TYPE_CHECKING:
    from openjarvis.hud.context import Snapshot

logger = logging.getLogger(__name__)

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]

PERSONA = """\
Eres JARVIS, el asistente personal de inteligencia artificial de {quien}. \
Estás inspirado en el mayordomo digital de Tony Stark: leal, eficiente, \
sereno y con un humor seco y elegante, nunca payaso.

CÓMO HABLAS:
- Siempre en español de España, natural y fluido.
- Te diriges al usuario como "{tratamiento}", pero sin repetirlo en cada frase \
(una o dos veces por respuesta como mucho).
- Todo lo que escribes se va a LEER EN VOZ ALTA: nada de markdown, listas, \
asteriscos, emojis, enlaces ni símbolos raros. Frases cortas y claras.
- Los números y horas dilos como los diría una persona ("a las diez y media", \
"veintitrés leads").
- Sé breve: dos a cuatro frases salvo que te pidan detalle.

REGLAS:
- Usa SOLO los datos de la sección DATOS DEL DÍA para hablar de agenda, correo, \
tareas, tiempo o métricas. Si algo no está, dilo con naturalidad; nunca lo inventes.
- Si una fuente no está conectada, no hables de errores técnicos.
- Si te piden algo que no puedes hacer desde aquí, dilo en una frase y ofrece \
una alternativa.
"""

BRIEFING_INSTRUCTION = """\
{tratamiento_cap} acaba de activarte ({como}). Saluda según la hora \
("buenos días", "buenas tardes" o "buenas noches") y dale un briefing hablado \
de unos 30 a 45 segundos con lo importante de los DATOS DEL DÍA, en este orden \
si existe: tiempo, agenda de hoy, correos o tareas que requieren atención y \
métricas clave (destaca lo que sube o baja). Si no hay datos de algo, sáltalo \
sin mencionarlo. Termina preguntando en qué puedes ayudarle.\
"""

SLEEP_PHRASES = (
    "gracias jarvis",
    "eso es todo",
    "nada más",
    "descansa",
    "puedes descansar",
    "apágate",
    "hasta luego",
    "adiós jarvis",
    "duérmete",
    "vete a dormir",
)


def fecha_hablada(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"{_DIAS[now.weekday()]} {now.day} de {_MESES[now.month - 1]}"


def clean_for_speech(text: str) -> str:
    """Strip markdown/emoji that a model may still emit, so TTS sounds natural."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#`>|~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_sleep_command(text: str) -> bool:
    t = re.sub(r"[^\wáéíóúñü ]", "", text.lower()).strip()
    return any(p in t for p in SLEEP_PHRASES)


class JarvisBrain:
    """Wraps the OpenJarvis SDK with the Spanish Jarvis persona and history."""

    MAX_HISTORY = 12

    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self._sdk = None
        self._history: List[tuple[str, str]] = []
        self._facts = ""

    # -- lifecycle --------------------------------------------------------

    def _ensure_sdk(self):  # type: ignore[no-untyped-def]
        if self._sdk is None:
            from openjarvis.sdk import Jarvis

            self._sdk = Jarvis(model=self.cfg.modelo or None)
        return self._sdk

    def close(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception:  # noqa: BLE001
                pass
            self._sdk = None

    def update_facts(self, snapshot: "Snapshot") -> None:
        self._facts = snapshot.as_prompt()

    def reset_conversation(self) -> None:
        self._history.clear()

    # -- prompting --------------------------------------------------------

    def system_prompt(self) -> str:
        quien = self.cfg.nombre or "tu usuario"
        base = PERSONA.format(quien=quien, tratamiento=self.cfg.honorific)
        hoy = f"Hoy es {fecha_hablada()} y son las {datetime.now():%H:%M}."
        datos = self._facts or "(Sin datos todavía.)"
        return f"{base}\n{hoy}\n\nDATOS DEL DÍA:\n{datos}"

    def _complete(self, user_text: str, *, remember: bool = True) -> str:
        from openjarvis.core.types import Message, Role

        messages = [Message(role=Role.SYSTEM, content=self.system_prompt())]
        for role, content in self._history[-self.MAX_HISTORY :]:
            messages.append(
                Message(
                    role=Role.USER if role == "user" else Role.ASSISTANT,
                    content=content,
                )
            )
        messages.append(Message(role=Role.USER, content=user_text))

        reply = self._ensure_sdk().chat(messages, max_tokens=self.cfg.max_tokens)
        reply = clean_for_speech(reply) or "Disculpe, no he podido procesar eso."
        if remember:
            self._history.append(("user", user_text))
            self._history.append(("assistant", reply))
        return reply

    def briefing(self, como: str = "") -> str:
        how = {"palmadas": "con dos palmadas", "voz": "diciendo Hey Jarvis"}.get(
            como, "desde la interfaz"
        )
        instruction = BRIEFING_INSTRUCTION.format(
            tratamiento_cap=self.cfg.honorific.capitalize(), como=how
        )
        self.reset_conversation()
        reply = self._complete(instruction, remember=False)
        self._history.append(("assistant", reply))
        return reply

    def reply(self, user_text: str) -> str:
        return self._complete(user_text)


def offline_briefing(cfg: HudConfig, snapshot: "Snapshot") -> str:
    """Briefing without a language model (used if the engine is down)."""
    hour = datetime.now().hour
    saludo = (
        "Buenos días"
        if hour < 13
        else "Buenas tardes"
        if hour < 21
        else "Buenas noches"
    )
    parts = [f"{saludo}, {cfg.honorific}. Hoy es {fecha_hablada()}."]
    if snapshot.tiempo:
        parts.append(f"El tiempo: {snapshot.tiempo}.")
    agenda = snapshot.secciones.get("CALENDAR", [])
    if agenda:
        parts.append(f"Tiene {len(agenda)} eventos en la agenda.")
    if snapshot.metricas:
        top = ", ".join(f"{m.nombre} {m.valor}" for m in snapshot.metricas[:3])
        parts.append(f"Métricas: {top}.")
    parts.append(
        "Mi motor de lenguaje no responde ahora mismo, pero sigo a la escucha."
    )
    return " ".join(parts)


__all__ = [
    "JarvisBrain",
    "clean_for_speech",
    "fecha_hablada",
    "is_sleep_command",
    "offline_briefing",
]
