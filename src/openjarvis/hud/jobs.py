"""Background jobs: elaborate work done by an expert model while Jarvis stays free.

Jarvis answers in about a second with a fast model. Work that takes longer —
writing or changing code, researching a topic, drafting a document, using the
mouse and keyboard — is handed to a :class:`JobManager`. A stronger model
(Claude Sonnet 5.5 by default) works on it in a thread with its own tools,
the HUD shows its progress in the "Tareas" panel, and Jarvis says the result
out loud when it is done. Meanwhile you can keep talking to Jarvis.

Safety: files are only read and written inside the user's home folder, and
commands that delete, overwrite history, shut down or change the system are
refused (the expert is told to ask the user to do those).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

TYPES = ("programar", "investigar", "redactar", "ordenador")
MAX_STEPS = 40
MAX_READ = 60_000

# Models with adaptive thinking whose progress notes can be shown, and which
# accept the server-side fallback for policy declines.
_RICH_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable", "claude-opus-5")

_DANGEROUS = re.compile(
    r"\b(?:rm|rmdir|rd|del|erase|remove-item|format|diskpart|shutdown|"
    r"restart-computer|stop-computer|reg\s+delete|remove-itemproperty|"
    r"set-executionpolicy|bcdedit|cipher|takeown|icacls|net\s+user|"
    r"clear-recyclebin|clear-content)\b"
    r"|git\s+(?:push|reset\s+--hard|clean|checkout\s+--|branch\s+-D)"
    r"|\|\s*(?:iex|invoke-expression)\b",
    re.I,
)

SYSTEM = """\
Eres el equipo de trabajo de JARVIS, el asistente personal de {quien}, en un \
PC con Windows (en español). Haces tareas elaboradas en segundo plano mientras \
Jarvis sigue atendiendo al usuario. Trabaja de forma completa, cuidada y \
rápida: planifica brevemente y ejecuta, sin pedir permiso para cada paso.

Carpeta de trabajo: {carpeta}
Hoy es {fecha}.

{especifico}

Normas:
- Solo puedes leer y escribir dentro de la carpeta personal del usuario.
- Nunca borres archivos ni ejecutes comandos destructivos: si hace falta, \
explícalo en el resumen para que lo haga el usuario.
- Nunca escribas contraseñas, códigos ni datos de pago.
- Al terminar, responde SOLO con un resumen de dos a cuatro frases en español \
para leerlo en voz alta (sin markdown ni listas): qué has hecho y dónde está \
el resultado. Nombra la carpeta de forma corta («en Documentos, carpeta \
Jarvis»); nunca leas rutas completas.\
"""

SPECIFIC = {
    "programar": """\
TAREA DE PROGRAMACIÓN: escribe código completo, limpio y funcional (nada de \
«TODO» ni trozos a medias). Lee primero el código existente si lo hay. \
Usa la terminal (PowerShell) para probarlo cuando se pueda (por ejemplo \
`python archivo.py`, `npm test`) y corrige los errores que veas. Si creas un \
proyecto nuevo, incluye un README corto con cómo ejecutarlo.""",
    "investigar": """\
TAREA DE INVESTIGACIÓN: busca en internet, contrasta varias fuentes fiables y \
recientes, y guarda un informe claro en Markdown (informe.md) en la carpeta, \
con las fuentes al final. El resumen hablado debe dar la conclusión.""",
    "redactar": """\
TAREA DE REDACCIÓN: escribe el documento completo y bien estructurado en la \
carpeta (Markdown o .txt salvo que pidan otro formato). Cuida el tono que \
pida el usuario.""",
    "ordenador": """\
TAREA EN EL ORDENADOR: hazla tú con el ratón y el teclado. Empieza con una \
captura de pantalla, prefiere atajos de teclado fiables y comprueba cada paso \
con otra captura. No compres nada, no envíes nada a otras personas y no \
inicies sesión en nada: detente y explícalo.""",
}


@dataclass
class Job:
    tipo: str
    tarea: str
    carpeta: str
    titulo: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    estado: str = "en_curso"  # en_curso | hecha | fallo | cancelada
    notas: List[str] = field(default_factory=list)
    resultado: str = ""
    archivos: List[str] = field(default_factory=list)
    inicio: float = field(default_factory=time.time)
    fin: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tipo": self.tipo,
            "titulo": self.titulo,
            "tarea": self.tarea,
            "carpeta": self.carpeta,
            "estado": self.estado,
            "notas": self.notas[-6:],
            "resultado": self.resultado,
            "archivos": self.archivos[-12:],
            "inicio": self.inicio,
            "fin": self.fin,
        }


def documents_dir() -> Path:
    """The user's Documents folder (it may live in OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (Documents)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # type: ignore[attr-defined]
            if buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Documents"


def _slug(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:40] or "tarea"


def _inside_home(path: Path) -> bool:
    home = Path.home().resolve()
    try:
        path.resolve().relative_to(home)
    except ValueError:
        return False
    parts = {p.lower() for p in path.resolve().parts}
    return ".ssh" not in parts and ".openjarvis" not in parts


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
FILE_TOOLS = [
    _tool(
        "listar",
        "Lista archivos de una carpeta (rutas relativas a la carpeta de "
        "trabajo o absolutas). patron es un glob opcional, p. ej. '**/*.py'.",
        {"ruta": _S, "patron": _S},
        ["ruta"],
    ),
    _tool(
        "leer_archivo",
        "Lee un archivo de texto con números de línea.",
        {"ruta": _S},
        ["ruta"],
    ),
    _tool(
        "escribir_archivo",
        "Crea o reemplaza un archivo de texto con el contenido completo.",
        {"ruta": _S, "contenido": _S},
        ["ruta", "contenido"],
    ),
    _tool(
        "editar_archivo",
        "Sustituye un fragmento exacto (que aparezca una sola vez) por otro.",
        {"ruta": _S, "buscar": _S, "reemplazar": _S},
        ["ruta", "buscar", "reemplazar"],
    ),
]
TERMINAL_TOOL = _tool(
    "terminal",
    "Ejecuta un comando de PowerShell en la carpeta de trabajo (máx. 2 min) y "
    "devuelve la salida. Para instalar dependencias, ejecutar y probar código. "
    "No borres ni cambies la configuración del sistema.",
    {"comando": _S},
    ["comando"],
)
WEB_TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]
LABELS = {
    "listar": "Mirando la carpeta",
    "leer_archivo": "Leyendo",
    "escribir_archivo": "Escribiendo",
    "editar_archivo": "Editando",
    "terminal": "Ejecutando",
    "web_search": "Buscando en internet",
    "web_fetch": "Leyendo una web",
}


class JobManager:
    """Runs and tracks background jobs; reports progress through *publish*."""

    def __init__(
        self,
        cfg: Any,
        publish: Callable[..., None],
        on_finish: Callable[[Job], None],
        *,
        client: Callable[[], Any],
        computer: Callable[[], Any],
        hide_hud: Callable[[], None] = lambda: None,
        show_hud: Callable[[], None] = lambda: None,
    ) -> None:
        self.cfg = cfg
        self.publish = publish
        self.on_finish = on_finish
        self._client = client
        self._computer = computer
        self.hide_hud = hide_hud
        self.show_hud = show_hud
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._screen = threading.Lock()  # one job at a time on mouse/keyboard

    # -- public -----------------------------------------------------------

    def start(self, tipo: str, tarea: str, carpeta: str = "", titulo: str = "") -> Job:
        tipo = tipo if tipo in TYPES else "programar"
        titulo = (titulo or tarea).strip().split("\n")[0][:70]
        folder = self._folder(carpeta, titulo, tipo)
        job = Job(tipo=tipo, tarea=tarea.strip(), carpeta=str(folder), titulo=titulo)
        with self._lock:
            self._jobs[job.id] = job
        self._emit(job)
        threading.Thread(
            target=self._run, args=(job,), name=f"jarvis-job-{job.id}", daemon=True
        ).start()
        return job

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.inicio, reverse=True)
        return [j.to_dict() for j in jobs[:30]]

    def cancel(self, ident: str) -> bool:
        job = self._jobs.get(ident)
        if job is None or job.estado != "en_curso":
            return False
        job.cancel.set()
        return True

    def running(self) -> int:
        return sum(1 for j in self._jobs.values() if j.estado == "en_curso")

    # -- internals --------------------------------------------------------

    def _folder(self, carpeta: str, titulo: str, tipo: str) -> Path:
        if carpeta:
            path = Path(os.path.expandvars(os.path.expanduser(carpeta.strip('"'))))
            if not path.is_absolute():
                path = Path.home() / path
            if _inside_home(path):
                if tipo != "ordenador":
                    path.mkdir(parents=True, exist_ok=True)
                return path
        path = (
            documents_dir() / "Jarvis" / f"{time.strftime('%Y-%m-%d')}-{_slug(titulo)}"
        )
        if tipo != "ordenador":
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _emit(self, job: Job) -> None:
        self.publish("job", **job.to_dict())

    def _note(self, job: Job, text: str) -> None:
        text = " ".join(str(text).split())[:220]
        if text and (not job.notas or job.notas[-1] != text):
            job.notas.append(text)
            self._emit(job)

    def _tools(self, job: Job) -> List[dict]:
        if job.tipo == "ordenador":
            return [{"type": "computer_toolset_20260801"}]
        tools = list(FILE_TOOLS)
        if job.tipo == "programar":
            tools.append(TERMINAL_TOOL)
        if job.tipo in ("investigar", "redactar", "programar"):
            tools += WEB_TOOLS
        return tools

    def _request(self, job: Job, messages: List[dict]) -> Dict[str, Any]:
        from openjarvis.hud.brain import fecha_hablada

        model = getattr(self.cfg, "modelo_experto", "") or "claude-sonnet-5-5"
        system = SYSTEM.format(
            quien=self.cfg.nombre or "su usuario",
            carpeta=job.carpeta,
            fecha=fecha_hablada(),
            especifico=SPECIFIC[job.tipo],
        )
        req: Dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "tools": self._tools(job),
            "messages": messages,
        }
        if model.startswith(_RICH_MODELS):
            req["extra_headers"] = {
                "anthropic-beta": "server-side-fallback-2026-07-01,"
                "thinking-display-updates-2026-08-18"
            }
            req["extra_body"] = {
                "fallbacks": "default",
                "thinking": {"type": "adaptive", "display": "updates"},
                "output_config": {
                    "effort": getattr(self.cfg, "esfuerzo_experto", "medium")
                },
            }
        return req

    def _run(self, job: Job) -> None:
        if job.tipo == "ordenador":
            if not self._screen.acquire(timeout=1):
                self._note(job, "Esperando a que termine otra tarea en pantalla…")
                self._screen.acquire()
            self.hide_hud()
            time.sleep(0.4)
        messages: List[dict] = [{"role": "user", "content": job.tarea}]
        summary = ""
        try:
            for _ in range(MAX_STEPS):
                if job.cancel.is_set():
                    job.estado = "cancelada"
                    break
                resp = self._client().messages.create(**self._request(job, messages))
                if resp.stop_reason == "refusal":
                    raise RuntimeError("El modelo no ha querido hacer esta tarea.")
                content = [b.model_dump(exclude_none=True) for b in resp.content]
                for block in content:
                    block.pop("citations", None)
                    if block.get("type") == "thinking" and block.get("thinking"):
                        self._note(job, block["thinking"])
                    elif block.get("type") == "server_tool_use":
                        self._note(
                            job, LABELS.get(block.get("name", ""), "Trabajando…")
                        )
                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                if any(t.strip() for t in texts):
                    summary = " ".join(t.strip() for t in texts if t.strip())
                messages.append({"role": "assistant", "content": content})
                if resp.stop_reason == "pause_turn":
                    continue
                calls = [b for b in content if b.get("type") == "tool_use"]
                if not calls:
                    break
                messages.append(
                    {"role": "user", "content": self._run_calls(job, calls)}
                )
            else:
                summary = (
                    summary or "He llegado al límite de pasos sin terminar del todo."
                )
            if job.estado == "en_curso":
                job.estado = "hecha"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tarea %s falló: %s", job.id, exc)
            job.estado = "fallo"
            summary = f"No he podido terminarla: {str(exc)[:200]}"
        finally:
            if job.tipo == "ordenador":
                self.show_hud()
                self._screen.release()
        if job.estado == "cancelada":
            summary = summary or "Tarea cancelada."
        job.resultado = _plain(summary) or "Hecho."
        job.fin = time.time()
        self._emit(job)
        try:
            self.on_finish(job)
        except Exception:  # noqa: BLE001
            logger.exception("Aviso de fin de tarea fallido")

    def _run_calls(self, job: Job, calls: List[dict]) -> List[dict]:
        results: List[dict] = []
        failed = False
        for call in calls:
            name, args = call.get("name", ""), call.get("input") or {}
            block: Dict[str, Any] = {"type": "tool_result", "tool_use_id": call["id"]}
            if call.get("toolset_name"):
                block["toolset_name"] = call["toolset_name"]
            if failed:
                block["content"] = (
                    "Not executed: an earlier computer action in this turn failed."
                )
                block["is_error"] = True
                results.append(block)
                continue
            try:
                if call.get("toolset_name") == "computer":
                    out = self._computer().run(name, args)
                    block["content"] = (
                        [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": out["image"],
                                },
                            }
                        ]
                        if "image" in out
                        else [{"type": "text", "text": out.get("text", "OK")}]
                    )
                else:
                    label = LABELS.get(name, name)
                    if args.get("ruta"):
                        target = Path(str(args["ruta"])).name or str(args["ruta"])
                    else:
                        target = str(args.get("comando") or "")
                    self._note(job, f"{label}: {target[:90]}")
                    block["content"] = self._file_tool(job, name, args)
            except Exception as exc:  # noqa: BLE001
                failed = call.get("toolset_name") == "computer"
                block["content"] = f"Error: {exc}"
                block["is_error"] = True
            results.append(block)
        return results

    def _path(self, job: Job, ruta: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(str(ruta).strip('"'))))
        if not path.is_absolute():
            path = Path(job.carpeta) / path
        if not _inside_home(path):
            raise PermissionError(
                "Solo puedo usar archivos dentro de la carpeta del usuario."
            )
        return path

    def _file_tool(self, job: Job, name: str, a: Dict[str, Any]) -> str:
        if name == "listar":
            base = self._path(job, a.get("ruta") or ".")
            if not base.exists():
                return "La carpeta no existe."
            pattern = a.get("patron") or "*"
            skip = {"node_modules", ".git", ".venv", "__pycache__", "dist", "build"}
            items = []
            for p in base.glob(pattern):
                if skip & set(p.relative_to(base).parts):
                    continue
                items.append(str(p.relative_to(base)) + ("/" if p.is_dir() else ""))
                if len(items) >= 300:
                    break
            return "\n".join(sorted(items)) or "(vacía)"
        if name == "leer_archivo":
            path = self._path(job, a["ruta"])
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            out = "\n".join(f"{i + 1:5d}  {line}" for i, line in enumerate(lines))
            return out[:MAX_READ] + ("\n…(recortado)" if len(out) > MAX_READ else "")
        if name == "escribir_archivo":
            path = self._path(job, a["ruta"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(a["contenido"]), encoding="utf-8")
            self._remember_file(job, path)
            return f"Guardado {path} ({len(str(a['contenido']))} caracteres)."
        if name == "editar_archivo":
            path = self._path(job, a["ruta"])
            text = path.read_text(encoding="utf-8")
            old, new = str(a["buscar"]), str(a["reemplazar"])
            count = text.count(old)
            if count != 1:
                raise ValueError(
                    f"El fragmento aparece {count} veces; debe aparecer una sola."
                )
            path.write_text(text.replace(old, new), encoding="utf-8")
            self._remember_file(job, path)
            return f"Editado {path}."
        if name == "terminal":
            return run_safely(str(a["comando"]), Path(job.carpeta))
        raise ValueError(f"Herramienta desconocida: {name}")

    def _remember_file(self, job: Job, path: Path) -> None:
        if str(path) not in job.archivos:
            job.archivos.append(str(path))
            self._emit(job)


def run_safely(command: str, cwd: Path, timeout: float = 120) -> str:
    """Run a PowerShell command for a job, refusing destructive ones."""
    if _DANGEROUS.search(command):
        return (
            "Bloqueado por seguridad: los comandos que borran, cambian el "
            "sistema o el historial de git debe hacerlos el usuario. "
            "Explícale qué hacer."
        )
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        res = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"El comando tardó más de {int(timeout)} s y se ha detenido."
    out = ((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip()
    return (f"[código {res.returncode}]\n" + out[-6000:]).strip()


def _plain(text: str) -> str:
    """Make a summary speakable: no markdown, no long paths read letter by letter."""
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:700]


__all__ = ["Job", "JobManager", "TYPES", "documents_dir", "run_safely"]
