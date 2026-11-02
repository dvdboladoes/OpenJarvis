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

[cerebro]
modelo = ""              # Vacío = el modelo por defecto de OpenJarvis
max_tokens = 400

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

    modelo: str = ""
    max_tokens: int = 400

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

    d.modelo = str(cerebro.get("modelo", d.modelo) or "")
    d.max_tokens = int(_clamp(cerebro.get("max_tokens", d.max_tokens), 64, 4096, 400))

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


def load_hud_config(path: Optional[Path] = None) -> HudConfig:
    """Load ``jarvis.toml`` (creating it with defaults on first run)."""
    target = ensure_config_file(path)
    with open(target, "rb") as fh:
        data = tomllib.load(fh)
    return parse_hud_config(data, path=target)


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_TEMPLATE",
    "HudConfig",
    "config_path",
    "ensure_config_file",
    "load_hud_config",
    "parse_hud_config",
]
