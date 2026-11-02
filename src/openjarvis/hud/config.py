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
palmadas = true          # Dos palmadas seguidas lo despiertan
palabra_clave = true     # Decir "Hey Jarvis" lo despierta
sensibilidad_palmadas = 0.5   # 0.1 = solo palmadas muy fuertes, 0.9 = muy sensible
sensibilidad_palabra = 0.5    # 0.1 = estricto, 0.9 = se activa con facilidad

[voz]
motor = "edge"                    # "edge" (gratis), "elevenlabs", "openai_tts"...
voz = "es-ES-AlvaroNeural"        # edge: es-ES-AlvaroNeural, es-ES-ElviraNeural...
velocidad = 1.05
idioma_escucha = "es"             # Idioma que entiende al escucharte

[oido]
# "local" = Whisper en tu PC (gratis). "openai" = en la nube, el que mejor
# entiende (necesita la variable OPENAI_API_KEY; cuesta céntimos al día).
motor = "local"
# "auto" elige el mejor que tu PC mueve con soltura (large-v3-turbo con
# tarjeta NVIDIA, medium sin ella). Opciones: small, medium, large-v3-turbo
modelo = "auto"

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
max_tokens = 500
temperatura = 0.6

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

[conversacion]
# Tras el briefing se queda escuchando. Si pasan estas respuestas sin que
# hables, vuelve a reposo hasta la próxima palmada o "Hey Jarvis".
silencios_para_dormir = 2

[interfaz]
puerto = 8765
abrir_ventana = true
pantalla_completa = false
"""


@dataclass
class HudConfig:
    """Resolved HUD settings (defaults mirror :data:`DEFAULT_TEMPLATE`)."""

    nombre: str = ""
    tratamiento: str = "señor"
    ciudad: str = ""

    palmadas: bool = True
    palabra_clave: bool = True
    sensibilidad_palmadas: float = 0.5
    sensibilidad_palabra: float = 0.5

    motor_voz: str = "edge"
    voz: str = "es-ES-AlvaroNeural"
    velocidad: float = 1.05
    idioma_escucha: str = "es"

    motor_oido: str = "local"
    modelo_oido: str = "auto"

    modelo: str = ""
    motor_cerebro: str = "auto"
    modo_cerebro: str = "auto"
    contexto: int = 0
    max_tokens: int = 500
    temperatura: float = 0.6

    fuentes: List[str] = field(
        default_factory=lambda: ["gcalendar", "gmail", "google_tasks"]
    )
    horas_atras: float = 24
    incluir_tiempo: bool = True

    metricas_fuente: str = ""
    metricas_titulo: str = "Métricas"

    silencios_para_dormir: int = 2

    puerto: int = 8765
    abrir_ventana: bool = True
    pantalla_completa: bool = False

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
    ui = data.get("interfaz", {}) or {}

    d.nombre = str(usuario.get("nombre", d.nombre) or "")
    d.tratamiento = str(usuario.get("tratamiento", d.tratamiento) or "")
    d.ciudad = str(usuario.get("ciudad", d.ciudad) or "")

    d.palmadas = bool(activ.get("palmadas", d.palmadas))
    d.palabra_clave = bool(activ.get("palabra_clave", d.palabra_clave))
    d.sensibilidad_palmadas = _clamp(
        activ.get("sensibilidad_palmadas", d.sensibilidad_palmadas), 0.05, 0.95, 0.5
    )
    d.sensibilidad_palabra = _clamp(
        activ.get("sensibilidad_palabra", d.sensibilidad_palabra), 0.05, 0.95, 0.5
    )

    d.motor_voz = str(voz.get("motor", d.motor_voz) or "edge")
    d.voz = str(voz.get("voz", d.voz) or "")
    d.velocidad = _clamp(voz.get("velocidad", d.velocidad), 0.5, 2.0, 1.0)
    d.idioma_escucha = str(voz.get("idioma_escucha", d.idioma_escucha) or "")

    d.motor_oido = str(oido.get("motor", d.motor_oido) or "local").lower()
    d.modelo_oido = str(oido.get("modelo", d.modelo_oido) or "auto")

    d.modelo = str(cerebro.get("modelo", d.modelo) or "")
    d.motor_cerebro = str(cerebro.get("motor", d.motor_cerebro) or "auto")
    d.modo_cerebro = str(cerebro.get("modo", d.modo_cerebro) or "auto")
    d.contexto = int(_clamp(cerebro.get("contexto", 0) or 0, 0, 131072, 0))
    d.max_tokens = int(_clamp(cerebro.get("max_tokens", d.max_tokens), 64, 4096, 500))
    d.temperatura = _clamp(cerebro.get("temperatura", d.temperatura), 0.0, 1.5, 0.6)

    fuentes = briefing.get("fuentes", d.fuentes)
    d.fuentes = [str(s) for s in fuentes] if isinstance(fuentes, list) else d.fuentes
    d.horas_atras = _clamp(briefing.get("horas_atras", d.horas_atras), 1, 168, 24)
    d.incluir_tiempo = bool(briefing.get("incluir_tiempo", d.incluir_tiempo))

    d.metricas_fuente = str(metricas.get("fuente", d.metricas_fuente) or "")
    d.metricas_titulo = str(metricas.get("titulo", d.metricas_titulo) or "Métricas")

    d.silencios_para_dormir = int(
        _clamp(conv.get("silencios_para_dormir", d.silencios_para_dormir), 1, 10, 2)
    )

    d.puerto = int(_clamp(ui.get("puerto", d.puerto), 1024, 65535, 8765))
    d.abrir_ventana = bool(ui.get("abrir_ventana", d.abrir_ventana))
    d.pantalla_completa = bool(ui.get("pantalla_completa", d.pantalla_completa))
    return d


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
    "save_config_value",
]
