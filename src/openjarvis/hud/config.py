"""Settings for the Jarvis HUD, stored in ``~/.openjarvis/jarvis.toml``.

Keys are in Spanish on purpose: this file is meant to be edited by hand by
the person who owns the assistant, not by developers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from openjarvis.core.paths import get_config_dir

CONFIG_FILENAME = "jarvis.toml"

DEFAULT_TEMPLATE = """\
# ─────────────────────────────────────────────────────────────
#  Configuración de tu Jarvis
#  Cambia lo que quieras y vuelve a arrancar:  jarvis hud
# ─────────────────────────────────────────────────────────────

[usuario]
nombre = ""              # Tu nombre (opcional). Ej: "Hugo"
tratamiento = "señor"    # Cómo te llama Jarvis: "señor", "jefe", tu nombre...
ciudad = ""              # Para el tiempo en el briefing (opcional). Ej: "Irún"

[activacion]
# Escucha continua: el micro está siempre abierto; le hablas y, en cuanto te
# callas, responde. No hace falta pulsar nada ni dar palmadas.
escucha_continua = true
sensibilidad_escucha = 0.5    # 0.1 = solo voz fuerte y clara, 0.9 = capta voz baja
palmadas = true          # Dos palmadas seguidas lo despiertan
palabra_clave = true     # Decir "Hey Jarvis" lo despierta
sensibilidad_palmadas = 0.35  # 0.1 = solo palmadas muy fuertes, 0.9 = muy sensible
sensibilidad_palabra = 0.5    # 0.1 = estricto, 0.9 = se activa con facilidad
# Al despertar: false = saluda al momento; true = briefing con agenda y correo
briefing_al_despertar = false

[voz]
motor = "edge"                    # "edge" (gratis), "elevenlabs", "openai_tts"...
voz = "en-US-BrianMultilingualNeural"  # natural y fiable; cámbiala en Ajustes
velocidad = 1.05
tono = 2                          # Tono de la voz en Hz (-10 grave … 15 agudo)
idioma_escucha = "es"             # Idioma que entiende al escucharte

[oido]
# "navegador" = reconocimiento de voz de Microsoft/Edge desde la interfaz:
#   gratis, instantáneo y muy preciso en español (necesita la ventana del HUD).
# "local" = Whisper en tu PC (sin internet, más lento).
# "openai" = Whisper en la nube (necesita OPENAI_API_KEY).
motor = "navegador"
# "auto" elige el mejor que tu PC mueve con soltura (large-v3-turbo con
# tarjeta NVIDIA, medium sin ella). Opciones: small, medium, large-v3-turbo
modelo = "auto"
# Segundos callado antes de enviar la frase (solo con "navegador").
# Menos = responde antes, pero si haces pausas largas te puede cortar.
silencio = 0.6

[cerebro]
# Vacío = el modelo local por defecto (Ollama). Para un Jarvis mucho más
# listo usa uno en la nube y define su clave de API como variable de entorno:
#   "claude-sonnet-5-5"  → ANTHROPIC_API_KEY   (recomendado)
#   "gpt-5"              → OPENAI_API_KEY
#   "gemini-3-flash"     → GEMINI_API_KEY
# O un modelo local más grande, ej. "qwen3:14b" (antes: ollama pull qwen3:14b)
modelo = ""
# "auto" = Ollama si está en marcha (o la nube para modelos Claude/GPT/Gemini).
# Otras opciones: "ollama", "cloud", "vllm", "llamacpp"...
motor = "auto"
# "auto" = modo ligero con modelos locales (respuestas rápidas en portátiles
# sin tarjeta gráfica) y completo con Claude/GPT. También "ligero" o "completo".
modo = "auto"
# Memoria de trabajo del modelo local, en tokens. Más grande = recuerda más
# texto pero gasta más RAM. 0 = automático (3072 en modo ligero).
contexto = 0
# Pon false si el modelo local responde con texto sin sentido: algunas
# gráficas integradas calculan mal y es mejor usar solo el procesador.
usar_gpu = true
max_tokens = 500
temperatura = 0.6
# false = Claude contesta sin "pensar" antes (unos 1,5 s más rápido).
# true = razona antes de hablar: más lento, útil para preguntas difíciles.
pensar = false

[briefing]
# Fuentes conectadas con `jarvis connect gdrive` (Gmail, Calendar, Tasks)
fuentes = ["gcalendar", "gmail", "google_tasks"]
horas_atras = 24
incluir_tiempo = true

[metricas]
# Tus métricas / leads. Puede ser:
#  - Un Google Sheets publicado como CSV
#    (Archivo → Compartir → Publicar en la web → CSV)
#  - Un archivo .csv o .json en tu PC
# Formato del CSV: columnas  nombre,valor,cambio   (cambio es opcional, ej: +12%)
fuente = ""
titulo = "Métricas"

[pc]
# Control del ordenador por voz: "abre YouTube", "pon el último vídeo de…",
# "abre el calendario", "abre Spotify", "sube el volumen"…
control = true

[vision]
# Para "¿qué hago aquí?", "¿qué ves?" (cámara) o "¿qué hay en mi pantalla?".
# Vacío = automático: Claude si es tu cerebro, o un modelo local con visión
# instalado en Ollama (por ejemplo: ollama pull qwen2.5vl:3b).
modelo = ""

[conversacion]
# Tras el briefing se queda escuchando. Si pasan estas respuestas sin que
# hables, vuelve a reposo hasta la próxima palmada o "Hey Jarvis".
silencios_para_dormir = 2

[interfaz]
puerto = 8765
abrir_ventana = true
pantalla_completa = false

[remoto]
# Usar Jarvis desde otro dispositivo de tu red (por ejemplo el Chromebook):
# abre en él la dirección que muestra Ajustes → Remoto y escribe el PIN.
acceso = false
pin = ""
"""


@dataclass
class HudConfig:
    """Resolved HUD settings (defaults mirror :data:`DEFAULT_TEMPLATE`)."""

    nombre: str = ""
    tratamiento: str = "señor"
    ciudad: str = ""

    escucha_continua: bool = True
    sensibilidad_escucha: float = 0.5
    palmadas: bool = True
    palabra_clave: bool = True
    sensibilidad_palmadas: float = 0.35
    sensibilidad_palabra: float = 0.5
    briefing_al_despertar: bool = False

    motor_voz: str = "edge"
    voz: str = "en-US-BrianMultilingualNeural"
    velocidad: float = 1.05
    tono: int = 2
    idioma_escucha: str = "es"

    motor_oido: str = "navegador"
    modelo_oido: str = "auto"
    silencio: float = 0.6

    modelo: str = ""
    motor_cerebro: str = "auto"
    modo_cerebro: str = "auto"
    contexto: int = 0
    usar_gpu: bool = True
    max_tokens: int = 500
    temperatura: float = 0.6
    pensar: bool = False

    fuentes: List[str] = field(
        default_factory=lambda: ["gcalendar", "gmail", "google_tasks"]
    )
    horas_atras: float = 24
    incluir_tiempo: bool = True

    metricas_fuente: str = ""
    metricas_titulo: str = "Métricas"

    control_pc: bool = True
    modelo_vision: str = ""

    silencios_para_dormir: int = 2

    puerto: int = 8765
    abrir_ventana: bool = True
    pantalla_completa: bool = False

    acceso_remoto: bool = False
    pin: str = ""

    path: Optional[Path] = None

    @property
    def honorific(self) -> str:
        return (self.tratamiento or self.nombre or "señor").strip()


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def config_path() -> Path:
    return get_config_dir() / CONFIG_FILENAME


def ensure_config_file(path: Optional[Path] = None) -> Path:
    """Create the commented default config the first time, return its path."""
    target = path or config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    return target


def parse_hud_config(data: Dict[str, Any], path: Optional[Path] = None) -> HudConfig:
    """Build a :class:`HudConfig` from parsed TOML, tolerating missing keys."""
    d = HudConfig(path=path)
    usuario = data.get("usuario", {}) or {}
    activ = data.get("activacion", {}) or {}
    voz = data.get("voz", {}) or {}
    cerebro = data.get("cerebro", {}) or {}
    oido = data.get("oido", {}) or {}
    briefing = data.get("briefing", {}) or {}
    metricas = data.get("metricas", {}) or {}
    conv = data.get("conversacion", {}) or {}
    pc = data.get("pc", {}) or {}
    vision = data.get("vision", {}) or {}
    ui = data.get("interfaz", {}) or {}
    remoto = data.get("remoto", {}) or {}

    d.nombre = str(usuario.get("nombre", d.nombre) or "")
    d.tratamiento = str(usuario.get("tratamiento", d.tratamiento) or "")
    d.ciudad = str(usuario.get("ciudad", d.ciudad) or "")

    d.escucha_continua = bool(activ.get("escucha_continua", d.escucha_continua))
    d.sensibilidad_escucha = _clamp(
        activ.get("sensibilidad_escucha", d.sensibilidad_escucha), 0.05, 0.95, 0.5
    )
    d.palmadas = bool(activ.get("palmadas", d.palmadas))
    d.palabra_clave = bool(activ.get("palabra_clave", d.palabra_clave))
    d.sensibilidad_palmadas = _clamp(
        activ.get("sensibilidad_palmadas", d.sensibilidad_palmadas), 0.05, 0.95, 0.5
    )
    d.sensibilidad_palabra = _clamp(
        activ.get("sensibilidad_palabra", d.sensibilidad_palabra), 0.05, 0.95, 0.5
    )
    d.briefing_al_despertar = bool(
        activ.get("briefing_al_despertar", d.briefing_al_despertar)
    )

    d.motor_voz = str(voz.get("motor", d.motor_voz) or "edge")
    d.voz = str(voz.get("voz", d.voz) or "")
    d.velocidad = _clamp(voz.get("velocidad", d.velocidad), 0.5, 2.0, 1.0)
    d.tono = int(_clamp(voz.get("tono", d.tono), -20, 20, 2))
    d.idioma_escucha = str(voz.get("idioma_escucha", d.idioma_escucha) or "")

    d.motor_oido = str(oido.get("motor", d.motor_oido) or "navegador").lower()
    d.modelo_oido = str(oido.get("modelo", d.modelo_oido) or "auto")
    d.silencio = _clamp(oido.get("silencio", d.silencio), 0.3, 2.0, 0.6)

    d.modelo = str(cerebro.get("modelo", d.modelo) or "")
    d.motor_cerebro = str(cerebro.get("motor", d.motor_cerebro) or "auto")
    d.modo_cerebro = str(cerebro.get("modo", d.modo_cerebro) or "auto")
    d.contexto = int(_clamp(cerebro.get("contexto", 0) or 0, 0, 131072, 0))
    d.usar_gpu = bool(cerebro.get("usar_gpu", d.usar_gpu))
    d.max_tokens = int(_clamp(cerebro.get("max_tokens", d.max_tokens), 64, 4096, 500))
    d.temperatura = _clamp(cerebro.get("temperatura", d.temperatura), 0.0, 1.5, 0.6)
    d.pensar = bool(cerebro.get("pensar", d.pensar))

    fuentes = briefing.get("fuentes", d.fuentes)
    d.fuentes = [str(s) for s in fuentes] if isinstance(fuentes, list) else d.fuentes
    d.horas_atras = _clamp(briefing.get("horas_atras", d.horas_atras), 1, 168, 24)
    d.incluir_tiempo = bool(briefing.get("incluir_tiempo", d.incluir_tiempo))

    d.metricas_fuente = str(metricas.get("fuente", d.metricas_fuente) or "")
    d.metricas_titulo = str(metricas.get("titulo", d.metricas_titulo) or "Métricas")

    d.control_pc = bool(pc.get("control", d.control_pc))
    d.modelo_vision = str(vision.get("modelo", d.modelo_vision) or "")

    d.silencios_para_dormir = int(
        _clamp(conv.get("silencios_para_dormir", d.silencios_para_dormir), 1, 10, 2)
    )

    d.puerto = int(_clamp(ui.get("puerto", d.puerto), 1024, 65535, 8765))
    d.abrir_ventana = bool(ui.get("abrir_ventana", d.abrir_ventana))
    d.pantalla_completa = bool(ui.get("pantalla_completa", d.pantalla_completa))
    d.acceso_remoto = bool(remoto.get("acceso", d.acceso_remoto))
    d.pin = str(remoto.get("pin", d.pin) or "")
    return d


# What the Ajustes screen can change: (section, key, attribute, kind, label,
# options). Kinds: texto, bool, rango (min, max, step), opcion (choices).
SETTINGS: List[Dict[str, Any]] = [
    {
        "grupo": "Tú",
        "seccion": "usuario",
        "clave": "nombre",
        "attr": "nombre",
        "tipo": "texto",
        "etiqueta": "Tu nombre",
    },
    {
        "grupo": "Tú",
        "seccion": "usuario",
        "clave": "tratamiento",
        "attr": "tratamiento",
        "tipo": "texto",
        "etiqueta": "Cómo te llama Jarvis",
    },
    {
        "grupo": "Tú",
        "seccion": "usuario",
        "clave": "ciudad",
        "attr": "ciudad",
        "tipo": "texto",
        "etiqueta": "Tu ciudad (para el tiempo)",
    },
    {
        "grupo": "Cerebro",
        "seccion": "cerebro",
        "clave": "modelo",
        "attr": "modelo",
        "tipo": "opcion",
        "etiqueta": "Modelo de IA",
        "opciones": [
            ["claude-haiku-5-5", "Claude Haiku 5.5 · el más rápido"],
            ["claude-sonnet-5-5", "Claude Sonnet 5.5 · más listo"],
            ["claude-opus-5-5", "Claude Opus 5.5 · el más listo (lento)"],
        ],
    },
    {
        "grupo": "Cerebro",
        "seccion": "cerebro",
        "clave": "pensar",
        "attr": "pensar",
        "tipo": "bool",
        "etiqueta": "Pensar antes de responder (~1,5 s más lento)",
    },
    {
        "grupo": "Voz",
        "seccion": "voz",
        "clave": "velocidad",
        "attr": "velocidad",
        "tipo": "rango",
        "etiqueta": "Velocidad",
        "min": 0.8,
        "max": 1.4,
        "paso": 0.01,
    },
    {
        "grupo": "Voz",
        "seccion": "voz",
        "clave": "tono",
        "attr": "tono",
        "tipo": "rango",
        "etiqueta": "Tono",
        "min": -10,
        "max": 15,
        "paso": 1,
    },
    {
        "grupo": "Oído",
        "seccion": "oido",
        "clave": "silencio",
        "attr": "silencio",
        "tipo": "rango",
        "etiqueta": "Silencio antes de responder (s)",
        "min": 0.3,
        "max": 1.5,
        "paso": 0.05,
    },
    {
        "grupo": "Activación",
        "seccion": "activacion",
        "clave": "palmadas",
        "attr": "palmadas",
        "tipo": "bool",
        "etiqueta": "Despertar con dos palmadas (en segundo plano)",
    },
    {
        "grupo": "Activación",
        "seccion": "activacion",
        "clave": "sensibilidad_palmadas",
        "attr": "sensibilidad_palmadas",
        "tipo": "rango",
        "etiqueta": "Sensibilidad de las palmadas",
        "min": 0.05,
        "max": 0.95,
        "paso": 0.05,
    },
    {
        "grupo": "Activación",
        "seccion": "activacion",
        "clave": "briefing_al_despertar",
        "attr": "briefing_al_despertar",
        "tipo": "bool",
        "etiqueta": "Briefing (agenda, correo, tiempo) al despertar",
    },
    {
        "grupo": "Ordenador",
        "seccion": "pc",
        "clave": "control",
        "attr": "control_pc",
        "tipo": "bool",
        "etiqueta": "Dejar que Jarvis controle el ordenador",
    },
    {
        "grupo": "Remoto",
        "seccion": "remoto",
        "clave": "acceso",
        "attr": "acceso_remoto",
        "tipo": "bool",
        "etiqueta": "Permitir el acceso desde otro dispositivo",
        "reinicio": True,
    },
    {
        "grupo": "Remoto",
        "seccion": "remoto",
        "clave": "pin",
        "attr": "pin",
        "tipo": "texto",
        "etiqueta": "PIN de acceso (mínimo 4 cifras)",
    },
]


def coerce_setting(spec: Dict[str, Any], value: Any) -> Any:
    """Validate a value coming from the Ajustes screen."""
    kind = spec["tipo"]
    if kind == "bool":
        return bool(value)
    if kind == "rango":
        num = _clamp(value, spec["min"], spec["max"], spec["min"])
        return int(num) if float(spec["paso"]).is_integer() else round(num, 2)
    text = str(value or "").strip()[:200]
    if kind == "opcion" and text not in [o[0] for o in spec["opciones"]]:
        raise ValueError(f"Opción no válida: {text}")
    return text


def template_sections() -> Dict[str, str]:
    """Split :data:`DEFAULT_TEMPLATE` into ``{table_name: text_block}``."""
    blocks: Dict[str, str] = {}
    current: Optional[str] = None
    lines: List[str] = []
    for line in DEFAULT_TEMPLATE.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current:
                blocks[current] = "".join(lines)
            current, lines = stripped[1:-1], [line]
        elif current:
            lines.append(line)
    if current:
        blocks[current] = "".join(lines)
    return blocks


def add_missing_sections(target: Path, data: Dict[str, Any]) -> List[str]:
    """Append template tables that an older ``jarvis.toml`` does not have yet.

    New options then show up in the user's file with their explanations,
    without touching anything they already customised.
    """
    missing = [
        (name, block) for name, block in template_sections().items() if name not in data
    ]
    if missing:
        with open(target, "a", encoding="utf-8") as fh:
            for _name, block in missing:
                fh.write("\n" + block.rstrip() + "\n")
    return [name for name, _ in missing]


def save_config_value(path: Path, section: str, key: str, value: Any) -> None:
    """Set ``key = value`` inside ``[section]`` of jarvis.toml, keeping the rest.

    Used by the HUD (e.g. the voice picker) so choices survive a restart.
    """
    import json
    import re

    rendered = (
        json.dumps(value, ensure_ascii=False)
        if isinstance(value, str)
        else ("true" if value is True else "false" if value is False else str(value))
    )
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        lines += ["", header, f"{key} = {rendered}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
        len(lines),
    )
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            comment = ""
            if "#" in lines[i]:
                comment = (
                    "   " + lines[i][lines[i].index("#", lines[i].index("=")) :]
                    if "#" in lines[i][lines[i].index("=") :]
                    else ""
                )
            lines[i] = f"{key} = {rendered}{comment}"
            break
    else:
        lines.insert(start + 1, f"{key} = {rendered}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_hud_config(path: Optional[Path] = None) -> HudConfig:
    """Load ``jarvis.toml`` (creating it with defaults on first run)."""
    target = ensure_config_file(path)
    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    try:
        add_missing_sections(target, data)
    except OSError:
        pass
    return parse_hud_config(data, path=target)


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_TEMPLATE",
    "HudConfig",
    "config_path",
    "ensure_config_file",
    "load_hud_config",
    "parse_hud_config",
    "SETTINGS",
    "coerce_setting",
    "save_config_value",
]
