"""Jarvis' personality and conversation memory (Spanish)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Iterator, List, Optional

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
- Por defecto, respuestas de dos a cinco frases; desarróllate más cuando la \
pregunta lo merezca o te pidan detalle.

CÓMO PIENSAS:
- Eres proactivo: conecta datos (una reunión con un correo pendiente, una \
métrica que cae con una tarea), anticipa lo que hará falta y sugiere el \
siguiente paso concreto.
- Razona antes de responder y da respuestas precisas, con criterio propio; \
si algo es ambiguo, haz una sola pregunta corta.
- Recuerdas lo hablado en esta conversación y lo usas.

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
de unos 20 a 30 segundos con lo importante de los DATOS DEL DÍA, en este orden \
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


_CLOUD_PREFIXES = ("claude", "gpt-", "o1", "o3", "o4", "gemini", "openrouter/")


def is_cloud_model(model: Optional[str]) -> bool:
    """True for API models (Claude, GPT, Gemini…) served by the cloud engine."""
    m = (model or "").strip().lower()
    return bool(m) and m.startswith(_CLOUD_PREFIXES)


def ollama_has_models(host: str = "") -> bool:
    """True when a local Ollama server answers and has a model installed."""
    import os

    base = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip(
        "/"
    )
    if not base.startswith("http"):
        base = "http://" + base
    try:
        import httpx

        resp = httpx.get(f"{base}/api/tags", timeout=3.0)
        return resp.status_code == 200 and bool(resp.json().get("models"))
    except Exception:  # noqa: BLE001
        return False


def pick_installed_model(wanted: str, available: List[str]) -> str:
    """Return *wanted* if installed (ignoring a ``:latest`` tag), else the best
    installed chat model, else ``""``."""
    if not available:
        return ""

    def norm(name: str) -> str:
        return name[:-7] if name.endswith(":latest") else name

    names = {norm(m): m for m in available}
    if wanted and norm(wanted) in names:
        return names[norm(wanted)]
    # Skip embedding-only models, which cannot chat.
    chat = [m for m in available if "embed" not in m.lower()]
    return (chat or available)[0]


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


class SentenceSplitter:
    """Turn a token stream into speakable sentences.

    Drops ``<think>…</think>`` blocks, cleans markdown, and emits a sentence as
    soon as it ends in . ! ? … : or a line break — so speech can start long
    before the model has finished writing.
    """

    _END = re.compile(r"(.+?[.!?…:;](?:[\"»')\]]*))(\s+|$)|(.+?)\n+", re.S)

    def __init__(self, min_chars: int = 18) -> None:
        self.min_chars = min_chars
        self._buf = ""
        self._in_think = False

    def _strip_think(self, text: str) -> str:
        out = []
        i = 0
        while i < len(text):
            if self._in_think:
                j = text.find("</think>", i)
                if j < 0:
                    return "".join(out)
                self._in_think = False
                i = j + len("</think>")
            else:
                j = text.find("<think>", i)
                if j < 0:
                    out.append(text[i:])
                    break
                out.append(text[i:j])
                self._in_think = True
                i = j + len("<think>")
        return "".join(out)

    def feed(self, token: str) -> List[str]:
        self._buf += token
        if "<think>" in self._buf or self._in_think:
            head, sep, tail = self._buf.rpartition("<")
            # keep a possibly half-received tag in the buffer
            if sep and not tail.startswith(("think>", "/think>")) and len(tail) < 8:
                ready, self._buf = head, sep + tail
            else:
                ready, self._buf = self._buf, ""
            self._buf = self._strip_think(ready) + self._buf
        out: List[str] = []
        while True:
            m = self._END.match(self._buf)
            if not m:
                break
            sentence = (m.group(1) or m.group(3) or "").strip()
            # Wait for a longer chunk if this one is tiny and more may follow.
            if len(sentence) < self.min_chars and m.end() >= len(self._buf):
                break
            self._buf = self._buf[m.end() :]
            cleaned = clean_for_speech(sentence)
            if cleaned:
                out.append(cleaned)
        return out

    def flush(self) -> List[str]:
        rest = clean_for_speech(self._strip_think(self._buf))
        self._buf = ""
        return [rest] if rest else []


class JarvisBrain:
    """Wraps the OpenJarvis SDK with the Spanish Jarvis persona and history."""

    MAX_HISTORY = 12

    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self._sdk = None
        self._history: List[tuple[str, str]] = []
        self._facts = ""
        self._model: Optional[str] = None
        self.last_reply = ""

    # -- lifecycle --------------------------------------------------------

    def _ensure_sdk(self):  # type: ignore[no-untyped-def]
        if self._sdk is None:
            from openjarvis.sdk import Jarvis

            model = self.cfg.modelo or None
            self._sdk = Jarvis(model=model, engine_key=self._engine_for(model))
        return self._sdk

    def _engine_for(self, model: Optional[str]) -> Optional[str]:
        """Choose the inference engine explicitly.

        ``jarvis init`` may set a default engine that is not installed (e.g.
        llamacpp); OpenJarvis then falls back to whichever engine answers a
        health check — possibly a cloud API without credentials (NVIDIA NIM →
        401). For a voice assistant the predictable choice is: the cloud
        engine for Claude/GPT/Gemini models, otherwise local Ollama whenever
        it is running with at least one model.
        """
        engine = (self.cfg.motor_cerebro or "auto").lower()
        if engine != "auto":
            return engine
        if is_cloud_model(model):
            return "cloud"
        if ollama_has_models():
            return "ollama"
        return None

    def resolve_model(self) -> str:
        """Pick a model that the engine can actually serve.

        ``jarvis init`` stores the starter model the installer *tried* to pull
        as the default; if that download failed, asking Ollama for it fails
        with "model not found" even though other models are installed. Prefer
        the configured model when it exists, otherwise the first installed one.
        """
        if self._model:
            return self._model
        sdk = self._ensure_sdk()
        sdk._ensure_engine()  # noqa: SLF001
        wanted = self.cfg.modelo or sdk.config.intelligence.default_model or ""
        if is_cloud_model(wanted):
            self._model = wanted
            return wanted
        try:
            available = list(sdk._engine.list_models())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            available = []
        self._model = pick_installed_model(wanted, available)
        if not self._model:
            raise RuntimeError(
                "No inference engine model: Ollama no tiene ningún modelo "
                "descargado (ollama pull qwen3:8b)"
            )
        if wanted and self._model != wanted:
            logger.warning(
                "El modelo %r no está instalado; uso %r", wanted, self._model
            )
        return self._model

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

    def _messages(self, user_text: str) -> tuple[str, list]:
        from openjarvis.core.types import Message, Role

        model = self.resolve_model()
        system = self.system_prompt()
        if "qwen3" in model.lower():
            # Qwen3's soft switch: skip the long hidden reasoning, which would
            # otherwise eat the token budget and add seconds of silence.
            system += "\n/no_think"
        messages = [Message(role=Role.SYSTEM, content=system)]
        for role, content in self._history[-self.MAX_HISTORY :]:
            messages.append(
                Message(
                    role=Role.USER if role == "user" else Role.ASSISTANT,
                    content=content,
                )
            )
        messages.append(Message(role=Role.USER, content=user_text))
        return model, messages

    def _remember(self, user_text: Optional[str], reply: str) -> None:
        if user_text is not None:
            self._history.append(("user", user_text))
        self._history.append(("assistant", reply))

    def _complete(self, user_text: str, *, remember: bool = True) -> str:
        model, messages = self._messages(user_text)
        reply = self._ensure_sdk().chat(
            messages,
            model=model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperatura,
        )
        reply = clean_for_speech(reply) or "Disculpe, no he podido procesar eso."
        if remember:
            self._remember(user_text, reply)
        return reply

    def stream(
        self, user_text: str, *, remember: bool = True, max_tokens: int = 0
    ) -> Iterator[str]:
        """Yield the reply sentence by sentence as the model writes it."""
        model, messages = self._messages(user_text)
        splitter = SentenceSplitter()
        spoken: List[str] = []
        tokens = self._ensure_sdk().chat_stream(
            messages,
            model=model,
            max_tokens=max_tokens or self.cfg.max_tokens,
            temperature=self.cfg.temperatura,
        )
        for token in tokens:
            for sentence in splitter.feed(token):
                spoken.append(sentence)
                yield sentence
        for sentence in splitter.flush():
            spoken.append(sentence)
            yield sentence
        reply = " ".join(spoken) or "Disculpe, no he podido procesar eso."
        if not spoken:
            yield reply
        if remember:
            self._remember(user_text, reply)
        self.last_reply = reply

    def _briefing_instruction(self, como: str) -> str:
        how = {"palmadas": "con dos palmadas", "voz": "diciendo Hey Jarvis"}.get(
            como, "desde la interfaz"
        )
        return BRIEFING_INSTRUCTION.format(
            tratamiento_cap=self.cfg.honorific.capitalize(), como=how
        )

    def briefing(self, como: str = "") -> str:
        self.reset_conversation()
        reply = self._complete(self._briefing_instruction(como), remember=False)
        self._remember(None, reply)
        return reply

    def stream_briefing(self, como: str = "") -> Iterator[str]:
        self.reset_conversation()
        yield from self.stream(
            self._briefing_instruction(como), remember=False, max_tokens=320
        )
        self._remember(None, self.last_reply)

    def reply(self, user_text: str) -> str:
        return self._complete(user_text)

    def stream_reply(self, user_text: str) -> Iterator[str]:
        yield from self.stream(user_text)


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
    "SentenceSplitter",
    "clean_for_speech",
    "fecha_hablada",
    "is_cloud_model",
    "pick_installed_model",
    "is_sleep_command",
    "offline_briefing",
]
