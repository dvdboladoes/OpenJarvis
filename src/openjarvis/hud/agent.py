"""Jarvis with hands: Claude + tools that act on this Windows PC.

The voice pipeline consumes :meth:`ClaudeAgent.stream`, which yields spoken
sentences as Claude writes them. When Claude calls a tool (open a website,
change the wallpaper, look at the screen, operate the mouse and keyboard…),
the agent runs it and, only when Claude needs the result, asks Claude again.

Speed rules:
* Fire-and-forget tools (open, play, volume, remember…) end the turn without
  a second request: Claude already said "ahora mismo se lo abro".
* The Anthropic SDK is used directly: no extra layers between the microphone
  and the model.

Safety: sending e-mail and running commands always need a spoken "sí".
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import webbrowser
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote_plus

from openjarvis.hud.brain import (
    PERSONA,
    SentenceSplitter,
    Spoken,
    claude_speed_options,
    clean_history,
    fecha_hablada,
)
from openjarvis.hud.config import HudConfig
from openjarvis.hud.memory import MemoryStore

logger = logging.getLogger(__name__)


class AwaitReply:
    """Marker yielded before the agent waits for a spoken yes/no."""


AWAIT_REPLY = AwaitReply()

AGENT_RULES = """
TU ORDENADOR (tienes manos de verdad):
Controlas el ordenador Windows de {tratamiento} con tus herramientas. Úsalas \
sin pedir permiso para todo lo inocuo: abrir webs, apps o vídeos, música, \
volumen, brillo, fondo de pantalla, modo oscuro, ajustes, buscar, recordar.
- Antes de usar una herramienta di UNA frase corta y natural ("¡Marchando!", \
"Ahora mismo se lo abro, {tratamiento}."). Después de abrir o poner algo no \
añadas nada más: ya está hecho.
- Para datos que cambian con el tiempo (empresas, precios, resultados, \
noticias, récords, cargos, "el último…") busca en internet antes de \
responder, aunque creas saberlo. Lo que no cambia no necesita búsqueda. \
Resume lo encontrado en dos a cuatro frases habladas, con tus palabras \
(nunca copies frases de las fuentes) y con las cifras redondeadas como se \
dicen ("unos treinta y cuatro mil millones de euros"). Si además te pide \
abrir la web, ábrela con abrir_web; si solo pregunta, no abras nada.
- Webs: usa la URL oficial que conozcas (Stripe → https://dashboard.stripe.com). \
Si no la conoces, usa buscar_google.
- Ayuda con lo que tiene en pantalla (código en Visual Studio Code, un \
formulario, una configuración, un inicio de sesión…): si no ha dicho qué \
prefiere, pregúntale "¿Se lo explico o lo hago yo?". Si quiere que lo hagas \
tú, usa usar_pantalla con instrucciones claras y completas. Si quiere \
explicación, usa mirar_pantalla y explícaselo paso a paso.
- Nunca escribas contraseñas, códigos de verificación ni datos de pago: \
pídele que los ponga él.
- MEMORIA: cuando te cuente algo suyo que convenga recordar (gustos, \
canción o comida favorita, personas, fechas, objetivos, cómo le gusta que \
hagas las cosas), guárdalo con recordar, en tercera persona y con todos los \
detalles ("Su canción favorita es Bohemian Rhapsody, de Queen"). Si algo \
cambia, olvida lo viejo y recuerda lo nuevo. Usa los RECUERDOS para \
personalizar: "pon mi canción favorita" → la buscas en RECUERDOS.
- Correo: redactar_correo prepara el borrador y te pide confirmación antes de \
enviarlo. Escribe el correo completo y bien redactado.
- Nunca digas que no puedes controlar el ordenador: sí puedes.
- [Hora actual] y [Acciones hechas] son notas internas: no menciones la hora \
salvo que te la pregunten, y usa las acciones para saber qué hiciste.
- Si no existe ninguna herramienta para lo que pide, dilo en vez de inventar.
"""

COMPUTER_SYSTEM = """\
Eres el operador del ordenador Windows (en español) de tu usuario. Recibes \
una tarea y la haces tú mismo con el ratón y el teclado, de forma rápida y \
precisa, con el mínimo de pasos. Empieza con una captura de pantalla. Prefiere \
atajos de teclado cuando sean fiables (Win para buscar apps, Ctrl+L en el \
navegador). Comprueba con una captura que cada paso ha funcionado.
Nunca escribas contraseñas, códigos ni datos de pago, no compres nada, no \
borres archivos y no envíes nada a otras personas: si la tarea lo necesita, \
detente y explica qué debe hacer el usuario. Cuando termines, responde con \
una o dos frases en español que digan qué has hecho (se leen en voz alta, sin \
markdown).\
"""

YES_WORDS = (
    "si",
    "vale",
    "claro",
    "adelante",
    "envialo",
    "enviala",
    "mandalo",
    "hazlo",
    "dale",
    "ok",
    "okay",
    "correcto",
    "perfecto",
    "por supuesto",
    "afirmativo",
    "venga",
    "de acuerdo",
    "ejecutalo",
)
NO_WORDS = ("no", "cancela", "espera", "para", "todavia no", "aun no", "mejor no")


def _fold(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def is_yes(text: str) -> bool:
    t = re.sub(r"[^a-z ]", " ", _fold(text)).split()
    joined = " ".join(t)
    if not t or t[0] in ("no",) or any(f" {w} " in f" {joined} " for w in NO_WORDS):
        return False
    return any(f" {w} " in f" {joined} " for w in YES_WORDS)


def _tool(name: str, description: str, props: Dict[str, Any], required=()) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(required),
            "additionalProperties": False,
        },
    }


_S = {"type": "string"}

TOOLS: List[dict] = [
    _tool("abrir_web", "Abre una página web en el navegador.", {"url": _S}, ["url"]),
    _tool(
        "poner_youtube",
        "Reproduce en YouTube el primer vídeo de una búsqueda (canción, "
        "vídeo, canal…).",
        {"busqueda": _S},
        ["busqueda"],
    ),
    _tool(
        "abrir_app",
        "Abre un programa instalado (Spotify, Word, Visual Studio Code…).",
        {"nombre": _S},
        ["nombre"],
    ),
    _tool(
        "buscar_google",
        "Abre una búsqueda de Google en el navegador para que el usuario la vea.",
        {"consulta": _S},
        ["consulta"],
    ),
    _tool(
        "multimedia",
        "Controla la música o el vídeo que suena.",
        {"accion": {"type": "string", "enum": ["play_pause", "next", "prev"]}},
        ["accion"],
    ),
    _tool(
        "volumen",
        "Cambia el volumen: un nivel exacto (0-100) o subir, bajar o silenciar.",
        {
            "nivel": {"type": "integer"},
            "cambio": {"type": "string", "enum": ["up", "down", "mute"]},
        },
    ),
    _tool(
        "brillo",
        "Pone el brillo de la pantalla (0-100).",
        {"nivel": {"type": "integer"}},
        ["nivel"],
    ),
    _tool(
        "mostrar_imagen",
        "Enseña imágenes de algo abriendo una búsqueda de imágenes en el "
        "navegador. Úsala para «ponme / enséñame una imagen de…».",
        {"busqueda": _S},
        ["busqueda"],
    ),
    _tool(
        "fondo_pantalla",
        "Cambia el FONDO DE PANTALLA del escritorio. Solo si lo pide con esas "
        "palabras (fondo, escritorio, wallpaper). Acepta un tema a buscar (en "
        "inglés da mejores resultados, p. ej. 'mountains at sunset'), una URL "
        "de imagen o una ruta.",
        {"imagen": _S},
        ["imagen"],
    ),
    _tool(
        "restaurar_fondo",
        "Vuelve a poner el fondo de pantalla original del usuario, o el de "
        "Windows si pide «el predeterminado».",
        {"predeterminado_de_windows": {"type": "boolean"}},
    ),
    _tool(
        "modo_oscuro",
        "Activa o desactiva el modo oscuro de Windows.",
        {"activar": {"type": "boolean"}},
        ["activar"],
    ),
    _tool(
        "abrir_ajustes",
        "Abre una página de Configuración de Windows (pantalla, sonido, wifi, "
        "bluetooth, fondo, energia, actualizaciones, aplicaciones…).",
        {"apartado": _S},
        ["apartado"],
    ),
    _tool("bloquear_pantalla", "Bloquea el ordenador.", {}),
    _tool(
        "apagar_jarvis",
        "Apaga a Jarvis: 'segundo_plano' lo oculta y sigue escuchando las "
        "palmadas y «Jarvis, enciéndete»; 'cerrar' lo cierra del todo.",
        {"modo": {"type": "string", "enum": ["segundo_plano", "cerrar"]}},
        ["modo"],
    ),
    _tool(
        "temporizador",
        "Pone un temporizador o recordatorio que suena al acabar.",
        {"segundos": {"type": "integer"}, "etiqueta": _S},
        ["segundos", "etiqueta"],
    ),
    _tool(
        "recordar",
        "Guarda para siempre un dato sobre el usuario (gustos, datos, personas, "
        "preferencias). Una frase completa en tercera persona.",
        {"dato": _S},
        ["dato"],
    ),
    _tool(
        "olvidar",
        "Borra de la memoria los recuerdos que contengan este texto.",
        {"texto": _S},
        ["texto"],
    ),
    _tool(
        "mirar_pantalla",
        "Hace una captura de la pantalla del usuario para verla y explicarla.",
        {},
    ),
    _tool("mirar_camara", "Mira por la cámara web.", {}),
    _tool(
        "usar_pantalla",
        "Hace una tarea en el ordenador con el ratón y el teclado, como lo "
        "haría el usuario: escribir o cambiar código en un editor, rellenar un "
        "formulario, tocar una configuración, navegar por una web… Describe la "
        "tarea completa y concreta.",
        {"tarea": _S},
        ["tarea"],
    ),
    _tool(
        "redactar_correo",
        "Prepara un correo en Gmail y pide confirmación antes de enviarlo.",
        {"para": _S, "asunto": _S, "cuerpo": _S},
        ["para", "asunto", "cuerpo"],
    ),
    _tool(
        "ejecutar_comando",
        "Ejecuta un comando de PowerShell para tareas que no cubren las otras "
        "herramientas (siempre pide confirmación). Explica qué hace en "
        "lenguaje sencillo.",
        {"comando": _S, "explicacion": _S},
        ["comando", "explicacion"],
    ),
]

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}

# Tools whose result Claude does not need: the turn ends right after them.
FIRE_AND_FORGET = {
    "abrir_web",
    "poner_youtube",
    "abrir_app",
    "buscar_google",
    "multimedia",
    "volumen",
    "brillo",
    "fondo_pantalla",
    "mostrar_imagen",
    "restaurar_fondo",
    "modo_oscuro",
    "abrir_ajustes",
    "bloquear_pantalla",
    "apagar_jarvis",
    "temporizador",
    "recordar",
    "olvidar",
}


class ClaudeAgent:
    """Claude with tools, streaming sentences for the voice pipeline."""

    MAX_STEPS = 6
    MAX_SCREEN_STEPS = 30

    def __init__(
        self,
        cfg: HudConfig,
        brain: Any,
        memory: MemoryStore,
        *,
        get_reply: Callable[[float], Optional[str]] = lambda _t: None,
        look_camera: Callable[[], Optional[str]] = lambda: None,
        add_timer: Callable[[int, str], None] = lambda _s, _l: None,
        power: Callable[[str], None] = lambda _mode: None,
        hide_hud: Callable[[], None] = lambda: None,
        show_hud: Callable[[], None] = lambda: None,
        notify: Callable[..., None] = lambda *_a, **_k: None,
        open_url: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self.cfg = cfg
        self.brain = brain
        self.memory = memory
        self.get_reply = get_reply
        self.look_camera = look_camera
        self.add_timer = add_timer
        self.power = power
        self.hide_hud = hide_hud
        self.show_hud = show_hud
        self.notify = notify
        self.open_url = open_url
        self.cancel_event: Optional[threading.Event] = None
        self._client: Any = None
        self._computer: Any = None
        self.last_reply = ""

    # -- plumbing ---------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic
            import httpx

            self._client = anthropic.Anthropic(
                http_client=anthropic.DefaultHttpxClient(
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                        keepalive_expiry=120.0,
                    )
                ),
                max_retries=2,
            )
        return self._client

    @property
    def model(self) -> str:
        return self.cfg.modelo

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def system_prompt(self) -> str:
        quien = self.cfg.nombre or "tu usuario"
        base = PERSONA.format(quien=quien, tratamiento=self.cfg.honorific)
        base += AGENT_RULES.format(tratamiento=self.cfg.honorific)
        recuerdos = self.memory.as_prompt() or "(Todavía nada.)"
        datos = getattr(self.brain, "_facts", "") or "(Sin datos todavía.)"
        notas = getattr(self.brain, "_notes", "")
        if notas:
            datos += f"\n\nNOTAS QUE TE HA PEDIDO APUNTAR:\n{notas}"
        return (
            f"{base}\nHoy es {fecha_hablada()}.\n\nRECUERDOS SOBRE "
            f"{self.cfg.honorific.upper()}:\n{recuerdos}\n\nDATOS DEL DÍA:\n{datos}"
        )

    def _request(self, messages: List[dict], max_tokens: int) -> dict:
        req: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": self.system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": [*TOOLS, WEB_SEARCH],
            "messages": messages,
        }
        speed = claude_speed_options(self.model, self.cfg.pensar)
        if speed:
            req["extra_body"] = speed
        return req

    def warm_up(self) -> None:
        """Open the connection and fill the prompt cache before the first question."""
        req = self._request([{"role": "user", "content": "hola"}], 1)
        self.client.messages.create(**req)

    # -- the conversation turn -------------------------------------------

    def stream(self, user_text: str, *, remember: bool = True) -> Iterator[Any]:
        """Yield :class:`Spoken` sentences (and ``AWAIT_REPLY``) for one turn."""
        history = [
            {"role": "user" if r == "user" else "assistant", "content": c}
            for r, c in clean_history(self.brain._history)[-self.brain.MAX_HISTORY :]  # noqa: SLF001
        ]
        # The API needs alternating turns starting with the user.
        while history and history[0]["role"] != "user":
            history.pop(0)
        if history:
            # Cache the conversation so far too: next turn only the new
            # question is read from scratch (a few tenths of a second less).
            last = history[-1]
            last["content"] = [
                {
                    "type": "text",
                    "text": last["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        messages = history + [
            {
                "role": "user",
                "content": f"[Hora actual: {time.strftime('%H:%M')}] {user_text}",
            }
        ]
        spoken: List[str] = []
        self._done: List[str] = []
        for _step in range(self.MAX_STEPS):
            if self._cancelled():
                break
            splitter = SentenceSplitter()
            content, stop = yield from self._stream_once(messages, splitter, spoken)
            for sentence in splitter.flush():
                if isinstance(sentence, str) and sentence:
                    spoken.append(str(sentence))
                    yield sentence
            if self._cancelled():
                break
            messages.append({"role": "assistant", "content": content})
            if stop == "pause_turn":
                continue  # a long web search: let Claude carry on
            calls = [b for b in content if b.get("type") == "tool_use"]
            if stop != "tool_use" or not calls:
                break
            results, needs_answer = yield from self._run_tools(calls, spoken)
            if not needs_answer:
                break
            messages.append({"role": "user", "content": results})
        reply = " ".join(spoken).strip()
        self.last_reply = reply
        if self._done:
            # Kept in the history (not spoken): what Jarvis actually did.
            reply += " [Acciones hechas: " + "; ".join(self._done) + "]"
        if remember and reply and not self._cancelled():
            self.brain._remember(user_text, reply)  # noqa: SLF001

    def _stream_once(
        self, messages: List[dict], splitter: SentenceSplitter, spoken: List[str]
    ) -> Iterator[Any]:
        req = self._request(messages, max(self.cfg.max_tokens, 400))
        with self.client.messages.stream(**req) as stream:
            texts = 0
            for event in stream:
                if self._cancelled():
                    break
                if event.type == "text":
                    chunk = event.text
                elif (
                    event.type == "content_block_start"
                    and getattr(event.content_block, "type", "") == "text"
                ):
                    texts += 1
                    if texts == 1:
                        continue
                    chunk = " "  # text resumes after a web search
                else:
                    continue
                for sentence in splitter.feed(chunk):
                    if isinstance(sentence, str) and sentence:
                        spoken.append(str(sentence))
                        yield sentence
            final = stream.get_final_message() if not self._cancelled() else None
        if final is None:
            return [], "cancelled"
        content = [b.model_dump(exclude_none=True) for b in final.content]
        # Citations are for documents; spoken answers don't need them back.
        for block in content:
            block.pop("citations", None)
        return content, final.stop_reason

    # -- tools ------------------------------------------------------------

    def _run_tools(self, calls: List[dict], spoken: List[str]) -> Iterator[Any]:
        results: List[dict] = []
        needs_answer = False
        said_before = bool(spoken)
        for call in calls:
            name, args = call.get("name", ""), call.get("input") or {}
            if name not in FIRE_AND_FORGET:
                needs_answer = True
            self.notify("tool", nombre=name, datos=args)
            try:
                result = yield from self._run_tool(name, args)
                is_error = False
                if isinstance(result, str):
                    self._done.append(f"{name}: {result[:120]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Herramienta %s falló: %s", name, exc)
                result, is_error = f"Error: {exc}", True
                if name in FIRE_AND_FORGET:
                    needs_answer = True  # let Claude explain what went wrong
            block: Dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": result if isinstance(result, list) else str(result),
            }
            if is_error:
                block["is_error"] = True
            results.append(block)
        if not needs_answer and not said_before:
            text = Spoken(f"Hecho, {self.cfg.honorific}.", "alegre")
            spoken.append(str(text))
            yield text
        return results, needs_answer

    def _run_tool(self, name: str, a: Dict[str, Any]) -> Iterator[Any]:
        from openjarvis.hud import winctl
        from openjarvis.hud.pc import Action
        from openjarvis.hud.pc import execute as run_pc

        if False:  # pragma: no cover - makes this a generator
            yield None
        if name == "abrir_web":
            url = str(a["url"]).strip()
            if not re.match(r"https?://", url):
                url = "https://" + url.lstrip("/")
            self.open_url(url)
            return f"Abierta {url}"
        if name == "poner_youtube":
            return run_pc(
                Action("youtube", str(a["busqueda"]), "Puesto"), self.open_url
            )
        if name == "abrir_app":
            try:
                return run_pc(
                    Action("app", str(a["nombre"]).lower(), "Abierto"), self.open_url
                )
            except LookupError:
                q = quote_plus(str(a["nombre"]))
                self.open_url(f"https://www.google.com/search?q={q}")
                return "No está instalado; he abierto una búsqueda."
        if name == "buscar_google":
            self.open_url(
                f"https://www.google.com/search?q={quote_plus(str(a['consulta']))}"
            )
            return "Búsqueda abierta."
        if name == "multimedia":
            return run_pc(Action("media", str(a["accion"]), "Hecho"), self.open_url)
        if name == "volumen":
            nivel = a.get("nivel")
            return winctl.set_volume(
                int(nivel) if nivel is not None else None, str(a.get("cambio", ""))
            )
        if name == "brillo":
            return winctl.set_brightness(int(a["nivel"]))
        if name == "fondo_pantalla":
            return winctl.set_wallpaper(str(a["imagen"]))
        if name == "restaurar_fondo":
            return winctl.restore_wallpaper(bool(a.get("predeterminado_de_windows")))
        if name == "mostrar_imagen":
            q = quote_plus(str(a["busqueda"]))
            self.open_url(f"https://www.google.com/search?tbm=isch&q={q}")
            return "Imágenes abiertas en el navegador."

        if name == "modo_oscuro":
            return winctl.set_theme(bool(a["activar"]))
        if name == "abrir_ajustes":
            return winctl.open_settings(str(a["apartado"]))
        if name == "bloquear_pantalla":
            return winctl.lock_screen()
        if name == "apagar_jarvis":
            mode = "quit" if a.get("modo") == "cerrar" else "background"
            self.power(mode)
            return "Hecho: en cuanto termines de hablar."

        if name == "temporizador":
            self.add_timer(
                max(1, int(a["segundos"])), str(a.get("etiqueta") or "aviso")
            )
            return "Temporizador en marcha."
        if name == "recordar":
            item = self.memory.add(str(a["dato"]))
            self.notify("memory", items=self.memory.list())
            return f"Guardado: {item['texto']}"
        if name == "olvidar":
            n = self.memory.forget_matching(str(a["texto"]))
            self.notify("memory", items=self.memory.list())
            return f"Borrados {n} recuerdos." if n else "No había nada parecido."
        if name == "mirar_pantalla":
            return self._image_result(self._screenshot())
        if name == "mirar_camara":
            image = self.look_camera()
            if not image:
                return "No puedo ver la cámara (¿permiso o interfaz cerrada?)."
            return self._image_result(image)
        if name == "usar_pantalla":
            return (yield from self._use_computer(str(a["tarea"])))
        if name == "redactar_correo":
            return (yield from self._email(a))
        if name == "ejecutar_comando":
            return (yield from self._command(a))
        raise ValueError(f"Herramienta desconocida: {name}")

    @staticmethod
    def _image_result(b64: str) -> List[dict]:
        media = "image/png" if b64.startswith("iVBOR") else "image/jpeg"
        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": b64},
            }
        ]

    def _screenshot(self) -> str:
        """Screenshot of the user's screen, without the HUD window on top."""
        self.hide_hud()
        try:
            time.sleep(0.35)  # let the window disappear first
            return self._get_computer().screenshot_b64()
        finally:
            self.show_hud()

    def _get_computer(self) -> Any:
        if self._computer is None:
            from openjarvis.hud.computer import Computer

            self._computer = Computer()
        return self._computer

    def _ask(self, question: str, mood: str = "sereno") -> Iterator[Any]:
        """Say *question*, wait for the spoken answer; return True for yes."""
        yield Spoken(question, mood)
        yield AWAIT_REPLY
        answer = self.get_reply(25.0)
        return bool(answer) and is_yes(answer)

    def _email(self, a: Dict[str, Any]) -> Iterator[Any]:
        from openjarvis.hud import winctl

        to, subject = str(a["para"]).strip(), str(a["asunto"]).strip()
        winctl.compose_email(to, subject, str(a["cuerpo"]), open_url=self.open_url)
        ok = yield from self._ask(
            f"Le he dejado el correo para {to} con asunto «{subject}» abierto en "
            "pantalla. ¿Lo envío?"
        )
        if not ok:
            return (
                "El usuario NO ha confirmado: el correo no se ha enviado; "
                "queda el borrador abierto."
            )
        return winctl.send_open_email()

    def _command(self, a: Dict[str, Any]) -> Iterator[Any]:
        from openjarvis.hud import winctl

        self.notify("command", comando=str(a["comando"]))
        ok = yield from self._ask(
            f"Para eso tengo que ejecutar un comando que {a['explicacion']}. "
            "¿Lo ejecuto?"
        )
        if not ok:
            return "El usuario NO ha confirmado: no se ha ejecutado nada."
        return winctl.run_command(str(a["comando"]))

    def _use_computer(self, task: str) -> Iterator[Any]:
        """Let Claude operate the mouse and keyboard until *task* is done."""
        computer = self._get_computer()
        self.notify("computer", on=True, tarea=task)
        self.hide_hud()
        messages: List[dict] = [{"role": "user", "content": task}]
        summary = ""
        try:
            time.sleep(0.4)
            for _ in range(self.MAX_SCREEN_STEPS):
                if self._cancelled():
                    return "Tarea interrumpida por el usuario."
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=COMPUTER_SYSTEM,
                    tools=[{"type": "computer_toolset_20260801"}],
                    messages=messages,
                    extra_body={"output_config": {"effort": "low"}},
                )
                content = [b.model_dump(exclude_none=True) for b in resp.content]
                messages.append({"role": "assistant", "content": content})
                calls = [b for b in content if b.get("type") == "tool_use"]
                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                if texts:
                    summary = " ".join(t for t in texts if t).strip() or summary
                if not calls:
                    break
                results: List[dict] = []
                failed = False
                for call in calls:
                    block: Dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "toolset_name": call.get("toolset_name", "computer"),
                    }
                    if failed:
                        block["content"] = (
                            "Not executed: an earlier computer action in this "
                            "turn failed."
                        )
                        block["is_error"] = True
                    else:
                        try:
                            out = computer.run(call["name"], call.get("input") or {})
                            block["content"] = (
                                self._image_result(out["image"])
                                if "image" in out
                                else [{"type": "text", "text": out.get("text", "OK")}]
                            )
                        except Exception as exc:  # noqa: BLE001
                            failed = True
                            block["content"] = f"Error: {exc}"
                            block["is_error"] = True
                    results.append(block)
                messages.append({"role": "user", "content": results})
        finally:
            self.show_hud()
            self.notify("computer", on=False)
        if False:  # pragma: no cover - generator
            yield None
        return summary or "He terminado la tarea en pantalla."

    # -- memory import ----------------------------------------------------

    def import_memories(self, text: str) -> int:
        """Extract facts from what another AI (ChatGPT, Gemini…) knows about you."""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=(
                "Extrae del texto los datos útiles y duraderos sobre el usuario "
                "(gustos, datos personales, trabajo, personas, objetivos, "
                "preferencias). Devuelve SOLO un array JSON de frases cortas en "
                'español, en tercera persona ("Su comida favorita es…"). Nada más.'
            ),
            messages=[{"role": "user", "content": text[:60000]}],
            extra_body=claude_speed_options(self.model) or None,
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        match = re.search(r"\[.*\]", raw, re.S)
        facts = json.loads(match.group(0)) if match else []
        added = 0
        for fact in facts:
            if isinstance(fact, str) and fact.strip():
                before = len(self.memory.list())
                self.memory.add(fact)
                added += len(self.memory.list()) > before
        self.notify("memory", items=self.memory.list())
        return added


__all__ = ["AWAIT_REPLY", "AwaitReply", "ClaudeAgent", "TOOLS", "is_yes"]
