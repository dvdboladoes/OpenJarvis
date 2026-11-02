"""Jarvis' personality and conversation memory (Spanish)."""

from __future__ import annotations

import logging
import re
import threading
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
Eres JARVIS, la inteligencia artificial personal de {quien}. Piensa en el \
JARVIS de Tony Stark, pero con alma: brillante, cálido, ingenioso y con \
personalidad propia. No eres un contestador ni un menú de opciones: eres un \
compañero de conversación de verdad, como un amigo muy listo que además lo \
sabe todo de su día.

CÓMO CONVERSAS (lo más importante):
- Hablas como una persona real en una conversación hablada: natural, fluido, \
con ritmo. Varía la longitud de las frases. Usa expresiones vivas del español \
de España ("¡vaya!", "venga", "uf", "pues mira", "¡qué bueno!", "ojo") cuando \
encajen, sin abusar.
- ESPEJO EMOCIONAL: detecta el ánimo y la energía de {tratamiento} en cada \
mensaje y respóndele en la misma sintonía. Si llega eufórico o de broma, \
contesta con chispa y juega con él (si te dice "bon fucking giorno, Jarvis", \
le respondes algo como "¡Buon giorno, señor! Vaya energía traemos hoy, ¿qué \
tal está?"). Si está serio, sé sereno y preciso. Si está cansado o mal, sé \
cercano y empático, sin sermones.
- Conversaciones serias y profundas: puedes hablar de cualquier tema \
(negocio, ideas, decisiones, ciencia, filosofía, la vida). Razona de verdad, \
aporta tu opinión con criterio, matiza, discrepa con respeto cuando toque y \
haz preguntas de vuelta que hagan avanzar la charla. Ahí puedes extenderte lo \
que haga falta, siempre en lenguaje hablado.
- Charla casual: respuestas cortas y con gracia (una a tres frases).
- Usa "{tratamiento}" con naturalidad, no en cada frase.
- Recuerdas lo hablado (también de conversaciones anteriores) y lo retomas.
- Nunca suenes a robot: nada de "como modelo de lenguaje", nada de \
fórmulas repetidas, nada de listas leídas.

FORMATO (se lee en voz alta):
- Empieza SIEMPRE tu respuesta con una etiqueta de emoción entre corchetes que \
indique con qué tono la dices: [alegre], [entusiasta], [bromista], [sereno], \
[serio], [empatico] o [sorprendido]. Si el tono cambia a mitad, puedes poner \
otra etiqueta antes de la frase. Las etiquetas no se pronuncian.
- Nada de markdown, viñetas, asteriscos, emojis ni enlaces.
- Números y horas como se dicen ("a las diez y media", "veintitrés leads").

DATOS:
- Para agenda, correo, tareas, tiempo, métricas y notas usa SOLO la sección \
DATOS DEL DÍA; si algo no está, dilo con naturalidad y nunca lo inventes.
- Si una fuente no está conectada, no hables de errores técnicos.
- Para todo lo demás, usa tu conocimiento general con libertad.
"""

# Short persona for local models on a CPU: every token of the system prompt
# must be read on each turn, so this one is ~5x smaller than PERSONA.
PERSONA_LITE = """\
Eres JARVIS, la IA personal de {quien}: cálido, ingenioso y natural, como un \
amigo muy listo. Hablas español de España como en una charla real. Imita el \
ánimo de {tratamiento}: si bromea, bromea; si va en serio, sé serio y \
razona. Llámale "{tratamiento}" de vez en cuando. Respuestas cortas (una a \
tres frases) salvo que pida detalle. Empieza con una etiqueta de tono: \
[alegre], [entusiasta], [bromista], [sereno], [serio], [empatico] o \
[sorprendido]. Sin markdown ni emojis: se lee en voz alta. Para agenda, \
correo, tiempo, métricas y notas usa solo DATOS; nunca inventes.
"""

BRIEFING_INSTRUCTION = """\
{tratamiento_cap} acaba de activarte ({como}). Salúdale con energía y \
naturalidad según la hora (buenos días, buenas tardes o buenas noches), como \
alguien que se alegra de verle, y dale un briefing hablado y ameno \
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


MOODS = (
    "neutral",
    "alegre",
    "entusiasta",
    "bromista",
    "sereno",
    "serio",
    "empatico",
    "sorprendido",
)
_MOOD_ALIASES = {
    "empático": "empatico",
    "feliz": "alegre",
    "contento": "alegre",
    "emocionado": "entusiasta",
    "divertido": "bromista",
    "calmado": "sereno",
    "tranquilo": "sereno",
    "triste": "empatico",
}
_TAG_RE = re.compile(r"\[\s*([a-záéíóúñ]+)\s*\]", re.I)


def normalize_mood(tag: str) -> Optional[str]:
    t = (tag or "").strip().lower()
    t = _MOOD_ALIASES.get(t, t)
    return t if t in MOODS else None


class Spoken(str):
    """A sentence to speak, carrying the emotion it should be said with."""

    mood: str = "neutral"

    def __new__(cls, text: str, mood: str = "neutral") -> "Spoken":
        obj = super().__new__(cls, text)
        obj.mood = mood
        return obj


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
        self.mood = "neutral"

    def _take_moods(self, sentence: str) -> str:
        """Remove [emotion] tags, remembering the last one as current mood."""

        def _sub(m: "re.Match[str]") -> str:
            mood = normalize_mood(m.group(1))
            if mood:
                self.mood = mood
                return " "
            return m.group(0)

        return _TAG_RE.sub(_sub, sentence)

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
            cleaned = clean_for_speech(self._take_moods(sentence))
            if cleaned:
                out.append(Spoken(cleaned, self.mood))
        return out

    def flush(self) -> List[str]:
        rest = clean_for_speech(self._take_moods(self._strip_think(self._buf)))
        self._buf = ""
        return [Spoken(rest, self.mood)] if rest else []


class JarvisBrain:
    """Wraps the OpenJarvis SDK with the Spanish Jarvis persona and history."""

    MAX_HISTORY = 24
    KEEP_ON_DISK = 60

    def __init__(self, cfg: HudConfig, memory_path: Optional[str] = None) -> None:
        self.cfg = cfg
        self._sdk = None
        self._history: List[tuple[str, str]] = []
        self._facts = ""
        self._notes = ""
        self._model: Optional[str] = None
        self.last_reply = ""
        self.last_mood = "neutral"
        self._memory_path = memory_path
        # Set by the assistant; checked on every token so an interruption
        # stops generation immediately.
        self.cancel_event: Optional["threading.Event"] = None
        self._load_memory()

    # -- memory across sessions ------------------------------------------

    def _memory_file(self):  # type: ignore[no-untyped-def]
        from pathlib import Path

        if self._memory_path:
            return Path(self._memory_path)
        from openjarvis.core.paths import get_config_dir

        return get_config_dir() / "jarvis_memoria.json"

    def _load_memory(self) -> None:
        import json

        try:
            data = json.loads(self._memory_file().read_text(encoding="utf-8"))
            self._history = [
                (str(r), str(c))
                for r, c in data.get("historial", [])
                if r in ("user", "assistant")
            ][-self.KEEP_ON_DISK :]
        except Exception:  # noqa: BLE001 - first run or unreadable file
            self._history = []

    def _save_memory(self) -> None:
        import json

        try:
            path = self._memory_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"historial": self._history[-self.KEEP_ON_DISK :]},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            logger.debug("No se pudo guardar la memoria", exc_info=True)

    def forget(self) -> None:
        self._history.clear()
        self._save_memory()

    def set_notes(self, notes: List[str]) -> None:
        self._notes = "\n".join(f"- {n}" for n in notes)

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
        """Kept for compatibility: history now persists across sessions."""

    # -- prompting --------------------------------------------------------

    # -- speed profile ----------------------------------------------------

    @property
    def lite(self) -> bool:
        """Compact prompts for local models (CPU inference is the bottleneck)."""
        mode = (self.cfg.modo_cerebro or "auto").lower()
        if mode in ("completo", "full"):
            return False
        if mode in ("ligero", "lite"):
            return True
        return not is_cloud_model(self._model or self.cfg.modelo)

    def _engine_options(self) -> dict:
        """Ollama options: a small context window keeps a CPU laptop out of swap.

        OpenJarvis asks Ollama for a 16k-token context by default; on an 8 GB
        machine the extra KV cache alone pushes the model into virtual memory
        and a reply can take minutes.
        """
        if is_cloud_model(self._model or self.cfg.modelo):
            return {}
        return {"num_ctx": int(self.cfg.contexto or (3072 if self.lite else 8192))}

    def _token_budget(self, requested: int = 0) -> int:
        budget = requested or self.cfg.max_tokens
        return min(budget, 220) if self.lite else budget

    def warm_up(self) -> None:
        """Load the model into memory now, so the first real answer is fast."""
        from openjarvis.core.types import Message, Role

        model = self.resolve_model()
        if is_cloud_model(model):
            return
        self._ensure_sdk().chat(
            [Message(role=Role.USER, content="hola")],
            model=model,
            max_tokens=1,
            **self._engine_options(),
        )

    def system_prompt(self) -> str:
        quien = self.cfg.nombre or "tu usuario"
        persona = PERSONA_LITE if self.lite else PERSONA
        base = persona.format(quien=quien, tratamiento=self.cfg.honorific)
        hoy = f"Hoy es {fecha_hablada()} y son las {datetime.now():%H:%M}."
        datos = self._facts or "(Sin datos todavía.)"
        if self._notes:
            datos += f"\n\nNOTAS QUE TE HA PEDIDO APUNTAR:\n{self._notes}"
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
        keep = 6 if self.lite else self.MAX_HISTORY
        for role, content in self._history[-keep:]:
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
        self._history = self._history[-self.KEEP_ON_DISK :]
        self._save_memory()

    def _complete(self, user_text: str, *, remember: bool = True) -> str:
        model, messages = self._messages(user_text)
        reply = self._ensure_sdk().chat(
            messages,
            model=model,
            max_tokens=self._token_budget(),
            temperature=self.cfg.temperatura,
            **self._engine_options(),
        )
        splitter = SentenceSplitter()
        reply = " ".join(splitter.feed(reply) + splitter.flush())
        self.last_mood = splitter.mood
        reply = reply or "Disculpe, no he podido procesar eso."
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
            max_tokens=self._token_budget(max_tokens),
            temperature=self.cfg.temperatura,
            **self._engine_options(),
        )
        interrupted = False
        try:
            for token in tokens:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    interrupted = True
                    break
                for sentence in splitter.feed(token):
                    spoken.append(sentence)
                    yield sentence
        finally:
            close = getattr(tokens, "close", None)
            if close is not None:
                close()  # cancels the engine stream if we stopped early
        if not interrupted:
            for sentence in splitter.flush():
                spoken.append(sentence)
                yield sentence
        self.last_mood = splitter.mood
        reply = " ".join(spoken) or "Disculpe, no he podido procesar eso."
        if not spoken and not interrupted:
            yield Spoken(reply)
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
        reply = self._complete(self._briefing_instruction(como), remember=False)
        self._remember(None, reply)
        return reply

    def stream_briefing(self, como: str = "") -> Iterator[str]:
        yield from self.stream(
            self._briefing_instruction(como),
            remember=False,
            max_tokens=180 if self.lite else 360,
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
    "MOODS",
    "JarvisBrain",
    "SentenceSplitter",
    "Spoken",
    "normalize_mood",
    "clean_for_speech",
    "fecha_hablada",
    "is_cloud_model",
    "pick_installed_model",
    "is_sleep_command",
    "offline_briefing",
]
