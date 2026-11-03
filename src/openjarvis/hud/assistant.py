"""The always-on Jarvis: wake → briefing → hands-free conversation → sleep."""

from __future__ import annotations

import logging
import os
import queue
import random
import threading
import time
from typing import Any, Dict, Optional

from openjarvis.hud import window
from openjarvis.hud.agent import AWAIT_REPLY, ClaudeAgent
from openjarvis.hud.brain import (
    ActionRequest,
    JarvisBrain,
    Spoken,
    is_cloud_model,
    is_sleep_command,
    offline_briefing,
)
from openjarvis.hud.config import (
    SETTINGS,
    HudConfig,
    coerce_setting,
    save_config_value,
)
from openjarvis.hud.context import Snapshot, collect_snapshot
from openjarvis.hud.memory import MemoryStore
from openjarvis.hud.pc import Action, parse_command
from openjarvis.hud.pc import execute as run_pc_action
from openjarvis.hud.server import EventHub, HudServer
from openjarvis.hud.skills import NotesStore, TimerManager, parse_note, parse_timer
from openjarvis.hud.vision import (
    SUGGESTED_LOCAL,
    FrameBroker,
    capture_screen,
    parse_screen_watch,
    parse_vision,
    pick_vision_model,
)
from openjarvis.hud.voice import Ear, Speaker

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_AGE = 300  # seconds before the data is refreshed on wake
SNAPSHOT_REFRESH = 900  # background refresh period


def voice_catalog() -> list:
    """Voices offered in the HUD picker (free edge voices + paid if configured)."""
    from openjarvis.speech.edge_tts_backend import SPANISH_VOICES

    voices = [{"motor": "edge", "voz": v, "nombre": n} for v, n in SPANISH_VOICES]
    if os.environ.get("ELEVENLABS_API_KEY"):
        voices.insert(
            0,
            {
                "motor": "elevenlabs",
                "voz": os.environ.get("ELEVENLABS_VOICE_ID", ""),
                "nombre": "ElevenLabs · premium",
            },
        )
    if os.environ.get("OPENAI_API_KEY"):
        voices.append({"motor": "openai_tts", "voz": "onyx", "nombre": "OpenAI · Onyx"})
    return voices


_VERSION: Optional[str] = None


def _version() -> str:
    """Short git commit of this install, shown in Ajustes."""
    global _VERSION
    if _VERSION is None:
        from openjarvis.hud.launcher import repo_root, run_hidden

        try:
            res = run_hidden(
                ["git", "log", "-1", "--format=%h · %cd", "--date=short"],
                cwd=repo_root(),
                timeout=5,
            )
            _VERSION = res.stdout.strip() or "?"
        except Exception:  # noqa: BLE001
            _VERSION = "?"
    return _VERSION


class JarvisAssistant:
    def __init__(self, cfg: HudConfig, *, mic: bool = True) -> None:
        from openjarvis.core.paths import get_config_dir

        self.cfg = cfg
        self.hub = EventHub()
        self.brain = JarvisBrain(cfg)
        self.speaker = Speaker(cfg)
        self.ear = Ear(cfg, on_level=self._mic_level)
        self.notes = NotesStore(get_config_dir() / "jarvis_notas.json")
        self.timers = TimerManager(
            on_change=lambda items: self.hub.publish("timers", items=items),
            on_done=self._timer_done,
        )
        self.brain.set_notes(self.notes.texts())
        self.memory = MemoryStore(get_config_dir() / "jarvis_recuerdos.json")
        self.frames = FrameBroker(self.hub.publish)
        self.agent = ClaudeAgent(
            cfg,
            self.brain,
            self.memory,
            get_reply=self._wait_reply,
            look_camera=self._camera_frame,
            add_timer=lambda secs, label: self.timers.add(secs, label),
            hide_hud=self._hide_for_screen,
            show_hud=self._show_after_screen,
            notify=self.hub.publish,
        )
        self.server = HudServer(
            self.hub,
            {
                "activate": self._api_activate,
                "listen": self._api_listen,
                "ask": self._api_ask,
                "stop": self._api_stop,
                "refresh": self._api_refresh,
                "voice": self._api_voice,
                "timer": self._api_timer,
                "timer_cancel": self._api_timer_cancel,
                "note": self._api_note,
                "note_delete": self._api_note_delete,
                "mic": self._api_mic,
                "forget": self._api_forget,
                "frame": self._api_frame,
                "heard": self._api_heard,
                "screen_watch": self._api_screen_watch,
                "stt_unsupported": self._api_stt_unsupported,
                "config": self._api_config,
                "set_config": self._api_set_config,
                "memory": self._api_memory,
                "memory_add": self._api_memory_add,
                "memory_delete": self._api_memory_delete,
                "memory_update": self._api_memory_update,
                "memory_import": self._api_memory_import,
                "background": self._api_background,
                "wake": self._api_wake,
                "quit": self._api_quit,
                "update": self._api_update,
                "autostart": self._api_autostart,
                "history": self._api_history,
            },
            port=cfg.puerto,
            remote=cfg.acceso_remoto,
            pin=cfg.pin,
        )
        self._use_mic = mic
        self.listener: Any = None
        self._mic_enabled = True
        self._session = threading.Lock()
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._snapshot: Optional[Snapshot] = None
        self._snapshot_at = 0.0
        self._snapshot_lock = threading.Lock()
        self._state = ""
        self._mood = "neutral"
        self._screen_watch = False
        self._t_heard = 0.0
        self.brain.cancel_event = self._cancel
        self.agent.cancel_event = self._cancel
        self._awaiting = False
        self._replies: "queue.Queue[str]" = queue.Queue()
        self._background = False
        self._claps: Any = None
        self._hid_for_screen = False
        self.speaker.on_mp3 = self._remote_audio

    # ── public ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self.server.start()
        self._set_state("arrancando", "Iniciando sistemas…")
        self.hub.publish("voices", items=voice_catalog(), actual=self._voice_info())
        self.hub.publish("notes", items=self.notes.list())
        self.hub.publish("timers", items=self.timers.list())
        self.hub.publish("memory", items=self.memory.list())
        if self._use_mic:
            self._start_listener()
        for target, name in (
            (self._warm_up, "jarvis-warmup"),
            (self._refresh_loop, "jarvis-data"),
            (self._system_loop, "jarvis-system"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def stop(self) -> None:
        self._shutdown.set()
        self._cancel.set()
        self.speaker.stop()
        self.timers.stop()
        if self.listener is not None:
            self.listener.stop()
        if self._claps is not None:
            self._claps.stop()
        self.server.stop()
        self.brain.close()

    def wait(self) -> None:
        while not self._shutdown.wait(1.0):
            pass

    def wake(self, source: str = "interfaz") -> bool:
        """Start a session in the background; False if one is already running."""
        if self._session.locked():
            return False
        threading.Thread(
            target=self._run_session, args=(source,), name="jarvis-session", daemon=True
        ).start()
        return True

    # ── setup ──────────────────────────────────────────────────────────

    @property
    def continuous(self) -> bool:
        return (
            self.cfg.escucha_continua
            and self.listener is not None
            and ("continua" in self.listener.detectors)
        )

    @property
    def browser_ears(self) -> bool:
        return self.listener is not None and "navegador" in self.listener.detectors

    def _start_listener(self) -> None:
        if self.cfg.escucha_continua and self.cfg.motor_oido == "navegador":
            from openjarvis.hud.listening import BrowserListener

            self.listener = BrowserListener(self.hub.publish)
            self.listener.start()
            return
        if self.cfg.escucha_continua and self.cfg.motor_oido == "local":
            from openjarvis.hud.voice import _cuda_available

            if not _cuda_available():
                logger.warning("Whisper local sin gráfica NVIDIA: oído lento")
                self.hub.publish(
                    "notice",
                    text="Oído lento: estás usando Whisper sin gráfica. Pon "
                    'motor = "navegador" en [oido] para respuestas instantáneas.',
                )
        if self.cfg.escucha_continua:
            from openjarvis.hud.listening import ContinuousListener

            try:
                self.listener = ContinuousListener(
                    on_utterance=self._on_utterance,
                    on_level=self._mic_level,
                    on_speech_start=self._on_speech_start,
                    sensitivity=self.cfg.sensibilidad_escucha,
                )
                self.listener.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Micrófono no disponible: %s", exc)
                self.listener = None
            return

        from openjarvis.hud.triggers import ClapDetector, MicListener, WakeWordDetector

        clap = (
            ClapDetector(self.cfg.sensibilidad_palmadas) if self.cfg.palmadas else None
        )
        wake = None
        if self.cfg.palabra_clave:
            wake = WakeWordDetector(self.cfg.sensibilidad_palabra)
            try:
                wake.load()
            except Exception as exc:  # noqa: BLE001
                logger.warning("'Hey Jarvis' no disponible: %s", exc)
                self.hub.publish(
                    "notice",
                    text="La palabra 'Hey Jarvis' no está instalada. "
                    "Ejecuta: uv sync --extra desktop --extra hud",
                )
                wake = None

        try:
            self.listener = MicListener(
                on_wake=self.wake, on_level=self._mic_level, clap=clap, wake=wake
            )
            self.listener.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Micrófono no disponible: %s", exc)
            self.listener = None

    def _warm_up(self) -> None:
        status: Dict[str, Any] = {
            "continua": self.continuous,
            "activacion": self.listener.detectors if self.listener else [],
            "voz": self.cfg.motor_voz,
            "silencio": self.cfg.silencio,
        }
        try:
            if self.browser_ears:
                status["oido"] = "Navegador (Microsoft)"
                status["oido_navegador"] = True
            else:
                self.ear.warm_up()
                status["oido"] = self.ear.description if self.ear.ready else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reconocimiento de voz: %s", exc)
            status["oido"] = False
        try:
            self.speaker._resolve()  # noqa: SLF001 - pre-load the TTS backend
            status["voz"] = self.speaker.backend_name

            def _voice_ready() -> None:
                self.speaker.warm_up()
                self._prefetch_phrases()

            threading.Thread(
                target=_voice_ready, name="jarvis-voice-warmup", daemon=True
            ).start()
        except Exception:  # noqa: BLE001
            pass
        try:
            model = self.brain.resolve_model()
            self._set_state("arrancando", "Cargando el cerebro…")
            t0 = time.monotonic()
            if self.uses_agent:
                self.agent.warm_up()
            else:
                self.brain.warm_up()
            logger.info("Modelo %s listo en %.1f s", model, time.monotonic() - t0)
            if self.uses_agent:
                engine = "directo"
            else:
                sdk = self.brain._ensure_sdk()  # noqa: SLF001
                engine = getattr(sdk, "_resolved_engine_key", "") or "ok"
            status["motor"] = f"{model} · {engine}"
            status["nube"] = is_cloud_model(model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Motor de IA no disponible: %s", exc)
            status["motor"] = ""
            self._engine_notice(exc)
        self.hub.publish("status", **status)
        if not self._session.locked():
            self._set_state("reposo", self._idle_hint())

    @property
    def uses_agent(self) -> bool:
        """Claude gets the tool-using agent; local models keep the simple brain."""
        return (self.cfg.modelo or "").lower().startswith("claude")

    def _idle_hint(self) -> str:
        if self._background:
            return "En segundo plano"
        if self.continuous and self._mic_enabled:
            return "Le escucho: hable cuando quiera"
        ways = []
        if (
            self._mic_enabled
            and self.listener
            and "palmadas" in self.listener.detectors
        ):
            ways.append("dos palmadas")
        if self._mic_enabled and self.listener and "voz" in self.listener.detectors:
            ways.append("«Hey Jarvis»")
        ways.append("espacio para hablar")
        text = ", ".join(ways[:-1]) + (" o " if len(ways) > 1 else "") + ways[-1]
        return text[:1].upper() + text[1:]

    # ── data ───────────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        while not self._shutdown.is_set():
            self._get_snapshot(force=True)
            self._shutdown.wait(SNAPSHOT_REFRESH)

    def _system_loop(self) -> None:
        """Live CPU / RAM / battery for the HUD's system gauges."""
        try:
            import psutil
        except ImportError:
            return
        psutil.cpu_percent(None)
        while not self._shutdown.wait(2.0):
            try:
                battery = psutil.sensors_battery()
                self.hub.publish(
                    "system",
                    cpu=round(psutil.cpu_percent(None)),
                    ram=round(psutil.virtual_memory().percent),
                    bateria=round(battery.percent) if battery else None,
                    cargando=bool(battery.power_plugged) if battery else None,
                )
            except Exception:  # noqa: BLE001
                pass

    def _snapshot_fast(self) -> Optional[Snapshot]:
        """Use the data already loaded; refresh it in the background if stale.

        Collecting e-mail, calendar and weather can take seconds, which used to
        be added to every reply.
        """
        if self._snapshot is None:
            return self._get_snapshot()
        if time.monotonic() - self._snapshot_at > SNAPSHOT_MAX_AGE:
            if not self._snapshot_lock.locked():
                threading.Thread(
                    target=self._get_snapshot, kwargs={"force": True}, daemon=True
                ).start()
        return self._snapshot

    def _get_snapshot(self, force: bool = False) -> Snapshot:
        with self._snapshot_lock:
            fresh = time.monotonic() - self._snapshot_at < SNAPSHOT_MAX_AGE
            if self._snapshot is not None and fresh and not force:
                return self._snapshot
            try:
                snap = collect_snapshot(self.cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudieron recoger datos: %s", exc)
                snap = self._snapshot or Snapshot(
                    metricas_titulo=self.cfg.metricas_titulo
                )
            self._snapshot = snap
            self._snapshot_at = time.monotonic()
            self.brain.update_facts(snap)
            self.hub.publish("snapshot", **snap.to_dict())
            return snap

    # ── session ────────────────────────────────────────────────────────

    def _begin(self) -> bool:
        if not self._session.acquire(blocking=False):
            return False
        self._cancel.clear()
        if self.listener is not None:
            self.listener.pause()
        return True

    def _end(self) -> None:
        self.hub.publish("transcript", text="", final=True)
        self._set_mood("neutral")
        self._set_state("reposo", self._idle_hint())
        if self.listener is not None and self._mic_enabled:
            if self.continuous:
                time.sleep(0.3)  # let the speakers fall silent first
            self.listener.resume()
        self._session.release()

    def _run_session(self, source: str) -> None:
        if not self._begin():
            return
        try:
            self.hub.publish("wake", source=source)
            if source == "ptt":
                # Push-to-talk: straight to listening, no briefing.
                self._snapshot_fast()
                self._conversation(first_turn=True)
                return
            self._set_state("activando", "Sistemas en línea")
            if not self.cfg.briefing_al_despertar:
                # Instant: a canned greeting needs no language model.
                self._say(self._greeting())
                if not self.continuous:
                    self._conversation()
                return
            snap = self._snapshot_fast() or Snapshot()
            self._set_state("pensando", "")
            spoken = self._say_stream(
                self.brain.stream_briefing(source),
                fallback=lambda: offline_briefing(self.cfg, snap),
            )
            if spoken and not self.continuous:
                self._conversation()
        except Exception:  # noqa: BLE001
            logger.exception("Error en la sesión de Jarvis")
            self._set_state("error", "Algo ha fallado — revisa la consola")
            time.sleep(2)
        finally:
            self._end()

    def _on_speech_start(self) -> None:
        if not self._session.locked():
            self._set_state("escuchando", "Le escucho…")

    def _api_heard(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """The HUD page recognised a full sentence: answer it."""
        text = str(payload.get("text", "")).strip()[:2000]
        if not text or len(text) < 2:
            return {"ok": False}
        if self._background:
            # Hidden: only "Jarvis, enciéndete" wakes it up.
            if window.is_wake_phrase(text):
                threading.Thread(
                    target=self.exit_background, args=("voz",), daemon=True
                ).start()
                return {"ok": True}
            return {"ok": False, "background": True}
        if self._awaiting:
            self._awaiting = False
            self.hub.publish("message", role="user", text=text)
            self._replies.put(text)
            return {"ok": True}
        if self._session.locked():
            return {"ok": False, "busy": True}
        self._t_heard = time.monotonic()
        self._on_text(text)
        return {"ok": True}

    def _api_stt_unsupported(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        """The browser cannot do speech recognition: fall back to Whisper."""
        if not self.browser_ears:
            return {"ok": True}
        logger.warning("El navegador no reconoce voz; uso Whisper local")
        self.cfg.motor_oido = "local"
        self.listener = None
        if self._use_mic:
            self._start_listener()
        threading.Thread(target=self.ear.warm_up, daemon=True).start()
        return {"ok": True}

    def _on_text(self, text: str) -> None:
        def _handle() -> None:
            if not self._begin():
                return
            try:
                self.hub.publish("message", role="user", text=text)
                if is_sleep_command(text):
                    self._say(
                        Spoken(f"A su disposición, {self.cfg.honorific}.", "sereno")
                    )
                    return
                self._snapshot_fast()
                self._answer(text)
            except Exception:  # noqa: BLE001
                logger.exception("Error respondiendo")
            finally:
                self._end()

        threading.Thread(target=_handle, name="jarvis-heard", daemon=True).start()

    def _on_utterance(self, audio: Any) -> None:
        """Continuous mode: you finished a sentence → transcribe and answer."""

        def _handle() -> None:
            if not self._begin():
                return
            try:
                self._set_state("pensando", "")
                try:
                    text = self.ear.transcribe(audio) if self.ear.ready else ""
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No pude transcribir: %s", exc)
                    text = ""
                if not text or self._cancel.is_set():
                    return
                self.hub.publish("message", role="user", text=text)
                if is_sleep_command(text):
                    self._say(
                        Spoken(f"A su disposición, {self.cfg.honorific}.", "sereno")
                    )
                    return
                self._snapshot_fast()
                self._answer(text)
            except Exception:  # noqa: BLE001
                logger.exception("Error respondiendo")
            finally:
                self._end()

        threading.Thread(target=_handle, name="jarvis-utterance", daemon=True).start()

    def _conversation(self, first_turn: bool = False) -> None:
        silences = 0
        while not self._cancel.is_set() and not self._shutdown.is_set():
            self._set_state(
                "escuchando",
                "No le he oído bien, repita por favor…" if silences else "Le escucho…",
            )
            try:
                heard = self.ear.listen(
                    startup_silence=8.0 if first_turn else 6.0,
                    should_stop=self._cancel.is_set,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("No puedo escuchar: %s", exc)
                self.hub.publish("notice", text=str(exc))
                return
            first_turn = False
            if self._cancel.is_set():
                return
            if not heard:
                silences += 1
                if silences >= self.cfg.silencios_para_dormir:
                    return
                continue
            silences = 0
            self.hub.publish("message", role="user", text=heard)
            if is_sleep_command(heard):
                self._say(Spoken(f"A su disposición, {self.cfg.honorific}.", "sereno"))
                return
            self._answer(heard)

    def _answer(self, text: str) -> None:
        if self._try_skill(text):
            return
        if self._screen_watch:
            # Real-time screen mode: every question comes with a fresh look.
            self._look("pantalla", text)
            return
        self._set_state("pensando", "")
        self._say_stream(
            self.agent.stream(text)
            if self.uses_agent
            else self.brain.stream_reply(text),
            fallback=lambda: (
                "Lo siento, mi motor de lenguaje no responde en este momento."
            ),
        )

    def _try_skill(self, text: str) -> bool:
        """Instant skills: PC control, vision, timers and notes."""
        if self.cfg.control_pc:
            action = parse_command(text)
            if action is not None:
                return self._do_pc(action, text)
        watch = parse_screen_watch(text)
        if watch is not None:
            self._set_screen_watch(watch)
            self._say(
                Spoken(
                    "Entendido, ahora veo su pantalla en todo momento. Pregúnteme lo "
                    "que quiera sobre lo que está haciendo."
                    if watch
                    else "De acuerdo, dejo de mirar su pantalla.",
                    "sereno",
                )
            )
            return True
        vis = parse_vision(text)
        if vis is not None:
            self._look(*vis)
            return True
        return self._try_quick_skill(text)

    def _do_pc(self, action: Any, text: str = "") -> bool:
        """Run an instant PC command. Returns False to let Claude handle it."""
        if action.kind == "hud":
            self.hub.publish("camera", on=True)
            self._say(Spoken(f"{action.label}.", "alegre"))
            return True
        try:
            said = run_pc_action(action)
            self.hub.publish("pc", accion=action.kind, texto=said)
            self._say(Spoken(said, "alegre"))
        except LookupError as exc:
            if is_cloud_model(self.cfg.modelo) and text:
                # Not an installed program ("abre Stripe", "abre mi banco"):
                # let Claude work out the right website and open it.
                return False
            self._say(Spoken(f"{exc} ¿Me lo dices con otro nombre?", "sereno"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Acción del PC fallida: %s", exc)
            self._say(Spoken("No he podido hacerlo, lo siento.", "empatico"))
        return True

    def _run_requested_action(self, req: "ActionRequest") -> None:
        """Execute a ``<<kind:arg>>`` order that Claude wrote in its reply."""
        kinds = {"web": "url", "youtube": "youtube", "buscar": "search", "app": "app"}
        arg = req.arg
        if req.kind == "web" and not arg.lower().startswith(("http://", "https://")):
            arg = "https://" + arg.lstrip("/")
        action = Action(kinds[req.kind], arg, "")
        try:
            run_pc_action(action)
            self.hub.publish("pc", accion=action.kind, texto=f"{req.kind}: {req.arg}")
        except LookupError:
            # Unknown program: fall back to a web search for it.
            run_pc_action(Action("search", req.arg, ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Orden %s fallida: %s", req, exc)
            self.hub.publish("notice", text=f"No pude abrir {req.arg}.")

    def _vision_model(self) -> Optional[str]:
        if self.cfg.modelo_vision:
            return self.cfg.modelo_vision
        main = self.brain.resolve_model()
        if is_cloud_model(main):
            return main
        try:
            sdk = self.brain._ensure_sdk()  # noqa: SLF001
            installed = list(sdk._engine.list_models())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            installed = []
        return pick_vision_model(main, installed, is_cloud=False)

    def _look(self, source: str, question: str) -> None:
        """Answer about the camera or the screen."""
        self._set_state("pensando", "")
        model = self._vision_model()
        if not model:
            self._say(
                Spoken(
                    "Aún no tengo ojos, señor. Para verte necesito un modelo con "
                    "visión: instala uno con ollama pull "
                    f"{SUGGESTED_LOCAL.replace(':', ' dos puntos ')}, o usa Claude "
                    "como cerebro.",
                    "sereno",
                )
            )
            self.hub.publish(
                "notice",
                text=f"Para la visión: ollama pull {SUGGESTED_LOCAL} "
                '(o pon modelo = "claude-sonnet-5-5" en [cerebro]).',
            )
            return
        if source == "pantalla":
            try:
                image = capture_screen()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Captura de pantalla fallida: %s", exc)
                image = None
        else:
            self.hub.publish("camera", on=True)
            image = self.frames.request(timeout=8.0)
        if not image:
            where = "la cámara" if source == "camara" else "la pantalla"
            self._say(
                Spoken(
                    f"No consigo ver {where}. Revisa que la interfaz esté abierta y "
                    "que hayas permitido la cámara.",
                    "empatico",
                )
            )
            return
        self.hub.publish("seen", fuente=source)
        self._say_stream(
            self.brain.see(question, image, model, source),
            fallback=lambda: "No he podido analizar la imagen.",
        )

    def _try_quick_skill(self, text: str) -> bool:
        """Timers and notes are handled instantly, without the language model."""
        timer = parse_timer(text)
        if timer:
            seconds, label = timer
            self.timers.add(seconds, label)
            self._say(Spoken(f"Hecho. Temporizador de {label} en marcha.", "alegre"))
            return True
        note = parse_note(text)
        if note:
            self.notes.add(note)
            self.brain.set_notes(self.notes.texts())
            self.hub.publish("notes", items=self.notes.list())
            self._say(Spoken("Apuntado.", "alegre"))
            return True
        return False

    def _timer_done(self, timer: Any) -> None:
        self.hub.publish("alarm", label=timer.label)
        text = Spoken(
            f"{self.cfg.honorific.capitalize()}, el temporizador de "
            f"{timer.label} ha terminado.",
            "sorprendido",
        )
        if self._begin():
            try:
                self._say(text)
            finally:
                self._end()
        else:
            self.hub.publish("notice", text=str(text))

    def _say_stream(self, sentences: Any, fallback: Any) -> bool:
        """Speak sentences as the model produces them.

        Three stages run in parallel: the model writes sentence N+2 while the
        voice engine synthesises N+1 and the speakers play N. The first words
        are heard as soon as the first sentence exists instead of after the
        whole reply. Returns False if the engine failed before saying anything.
        """
        import queue

        text_q: "queue.Queue[Any]" = queue.Queue()
        audio_q: "queue.Queue[Any]" = queue.Queue(maxsize=3)
        done = object()
        t_start = time.monotonic()
        marks: Dict[str, float] = {}

        def produce() -> None:
            try:
                for sentence in sentences:
                    marks.setdefault("cerebro", time.monotonic())
                    if self._cancel.is_set():
                        break
                    if isinstance(sentence, ActionRequest):
                        # Open it right away, while Jarvis keeps talking.
                        threading.Thread(
                            target=self._run_requested_action,
                            args=(sentence,),
                            daemon=True,
                        ).start()
                        continue
                    text_q.put(sentence)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fallo del motor de IA: %s", exc)
                text_q.put(exc)
            finally:
                text_q.put(done)

        def synthesize() -> None:
            while True:
                item = text_q.get()
                if item is done or isinstance(item, Exception):
                    audio_q.put(item)
                    return
                if item is AWAIT_REPLY:
                    audio_q.put((item, None))
                    continue
                # A stream: playback starts with the first chunk of audio.
                audio_q.put((item, self.speaker.synthesize_streaming(item)))

        threading.Thread(target=produce, name="jarvis-llm", daemon=True).start()
        threading.Thread(target=synthesize, name="jarvis-tts", daemon=True).start()

        said: list[str] = []
        failed = False
        while True:
            try:
                item = audio_q.get(timeout=0.2)
            except queue.Empty:
                if self._cancel.is_set():
                    break  # interrupted: don't wait for the model to finish
                continue
            if item is done:
                break
            if isinstance(item, Exception):
                self._engine_notice(item)
                failed = True
                break
            if item[0] is AWAIT_REPLY:
                # Everything before the question has been said: listen now.
                self._awaiting = True
                self.hub.publish("confirm", pregunta=said[-1] if said else "")
                self._set_state("escuchando", "Le escucho…")
                if self.listener is not None and self._mic_enabled:
                    self.listener.resume()
                continue
            sentence, audio = item
            ready = getattr(audio, "ready", None)
            if ready is not None:
                ready.wait()  # time the first sound, not the request
            if "voz" not in marks:
                marks["voz"] = time.monotonic()
                self._report_timing(t_start, marks)
            said.append(str(sentence))
            self._set_mood(getattr(sentence, "mood", "neutral"))
            if self._state != "hablando":
                self._set_state("hablando", "")
            self.hub.publish("partial", role="jarvis", text=" ".join(said))
            if audio and not self._cancel.is_set():
                try:
                    self.speaker.play(audio, self._voice_level)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No se pudo reproducir el audio: %s", exc)

        if said:
            self.hub.publish(
                "message", role="jarvis", text=" ".join(said), mood=self._mood
            )
            return True
        if failed:
            self._say(fallback())
        return not failed

    def _report_timing(self, t_start: float, marks: Dict[str, float]) -> None:
        """Show where the waiting time went (brain vs. voice)."""
        brain = marks.get("cerebro", t_start) - t_start
        voice = marks.get("voz", t_start) - marks.get("cerebro", t_start)
        heard = self._t_heard if self._t_heard and self._t_heard <= t_start else t_start
        total = marks.get("voz", t_start) - heard
        self._t_heard = 0.0
        logger.info(
            "Tiempos: total %.1fs · cerebro %.1fs · voz %.1fs", total, brain, voice
        )
        self.hub.publish(
            "timing",
            total=round(total, 1),
            cerebro=round(brain, 1),
            voz=round(voice, 1),
        )

    def _say(self, text: str) -> None:
        mood = getattr(text, "mood", "neutral")
        self._set_mood(mood)
        self.hub.publish("message", role="jarvis", text=str(text), mood=mood)
        self._set_state("hablando", "")
        self.speaker.say(text, on_level=self._voice_level, mood=mood)

    def _engine_notice(self, exc: Exception) -> None:
        """Explain in plain Spanish why the language model is not answering."""
        msg = str(exc)
        low = msg.lower()
        model = self.cfg.modelo
        if msg.startswith(("Falta la clave", "Falta el conector")):
            hint = msg
        elif model and is_cloud_model(model):
            if "api" in low and "key" in low or "authentication" in low or "401" in low:
                hint = f"La clave de API para {model} no es válida o no está definida."
            elif "no inference engine" in low or "unavailable" in low:
                hint = (
                    f"No puedo usar {model}: falta la clave de API (variable de "
                    "entorno) o el paquete en la nube. Ejecuta: uv sync --extra "
                    "desktop --extra hud --extra inference-cloud --group desktop-native"
                )
            else:
                hint = f"El modelo {model} ha fallado: {msg[:160]}"
        elif "no inference engine" in low or "connect" in low or "refused" in low:
            hint = (
                "Ollama no está en marcha o no tiene ningún modelo. Abre Ollama "
                "y ejecuta: ollama pull qwen3:8b — o configura Claude en jarvis.toml."
            )
        elif "not found" in low and "model" in low:
            hint = f"El modelo '{model or 'por defecto'}' no está descargado en Ollama."
        else:
            hint = f"El motor de IA ha fallado: {msg[:160]}"
        self.hub.publish("notice", text=hint)

    # ── HUD API ────────────────────────────────────────────────────────

    def interrupt(self, timeout: float = 5.0) -> bool:
        """Stop whatever Jarvis is doing (speaking, thinking, listening).

        Returns True once the current session has ended and a new one can
        start, so the user is never locked out by a slow answer.
        """
        if not self._session.locked():
            return True
        self._cancel.set()
        self.speaker.stop()
        deadline = time.monotonic() + timeout
        while self._session.locked() and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._session.locked()

    def _api_activate(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self.interrupt()
        return {"started": self.wake("interfaz")}

    def _api_listen(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._state == "escuchando":
            return {"started": True}  # already listening to you
        self.interrupt()
        if self.browser_ears:
            # The page is always listening; just make sure it is switched on.
            self._mic_enabled = True
            self.listener.resume()
            return {"started": True}
        return {"started": self.wake("ptt")}

    def _run_job(self, job: Any) -> bool:
        def _wrapped() -> None:
            if not self._begin():
                return
            try:
                job()
            except Exception:  # noqa: BLE001
                logger.exception("Error en una acción del HUD")
            finally:
                self._end()

        if not self.interrupt():
            return False
        threading.Thread(target=_wrapped, daemon=True).start()
        return True

    def _api_ask(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("text", "")).strip()[:4000]
        if not text:
            return {"started": False}

        def _job() -> None:
            self._snapshot_fast()
            self.hub.publish("message", role="user", text=text)
            self._answer(text)

        return {"started": self._run_job(_job)}

    def _api_stop(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        threading.Thread(target=self.interrupt, daemon=True).start()
        return {"stopped": True}

    def _api_refresh(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        threading.Thread(
            target=self._get_snapshot, kwargs={"force": True}, daemon=True
        ).start()
        return {"refreshing": True}

    def _voice_info(self) -> Dict[str, str]:
        return {"motor": self.cfg.motor_voz, "voz": self.cfg.voz}

    def _api_voice(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        motor = str(payload.get("motor", "")).strip()
        voz = str(payload.get("voz", "")).strip()
        if motor or voz:
            self.speaker.set_voice(motor, voz)
            if self.cfg.path is not None:
                if motor:
                    save_config_value(self.cfg.path, "voz", "motor", motor)
                if voz:
                    save_config_value(self.cfg.path, "voz", "voz", voz)
            self.hub.publish("voices", items=voice_catalog(), actual=self._voice_info())
        if payload.get("probar"):
            sample = Spoken(
                f"¡Hola, {self.cfg.honorific}! ¿Qué tal suena esta voz? "
                "Yo diría que bastante bien.",
                "alegre",
            )
            return {"started": self._run_job(lambda: self._say(sample))}
        return self._voice_info()

    def _api_timer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        seconds = int(float(payload.get("segundos", 0) or 0))
        if seconds <= 0:
            return {"ok": False}
        mins = seconds // 60
        label = payload.get("etiqueta") or (
            f"{mins} minuto{'s' if mins != 1 else ''}"
            if seconds % 60 == 0
            else f"{seconds} segundos"
        )
        return {"id": self.timers.add(seconds, str(label)).id}

    def _api_timer_cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": self.timers.cancel(str(payload.get("id", "")))}

    def _api_note(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("texto", "")).strip()[:500]
        if not text:
            return {"ok": False}
        note = self.notes.add(text)
        self.brain.set_notes(self.notes.texts())
        self.hub.publish("notes", items=self.notes.list())
        return note

    def _api_note_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self.notes.delete(str(payload.get("id", "")))
        self.brain.set_notes(self.notes.texts())
        self.hub.publish("notes", items=self.notes.list())
        return {"ok": ok}

    def _api_mic(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Privacy switch: stop listening for claps / wake word."""
        self._mic_enabled = bool(payload.get("on", True))
        if self.listener is not None:
            if self._mic_enabled and not self._session.locked():
                self.listener.resume()
            elif not self._mic_enabled:
                self.listener.pause()
        self.hub.publish("mic", on=self._mic_enabled)
        if not self._session.locked():
            self._set_state("reposo", self._idle_hint())
        return {"on": self._mic_enabled}

    def _set_screen_watch(self, on: bool) -> None:
        self._screen_watch = bool(on)
        self.hub.publish("screen_watch", on=self._screen_watch)

    def _api_screen_watch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._set_screen_watch(bool(payload.get("on", not self._screen_watch)))
        return {"on": self._screen_watch}

    def _api_frame(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        image = str(payload.get("image", ""))[:8_000_000]
        return {"ok": self.frames.deliver(str(payload.get("id", "")), image)}

    def _api_forget(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self.brain.forget()
        self.hub.publish("notice", text="Memoria de conversación borrada.")
        return {"ok": True}

    # ── instant replies, confirmations and the agent's hooks ─────────────

    def _greetings(self, hour: Optional[int] = None) -> list:
        hour = time.localtime().tm_hour if hour is None else hour
        if 6 <= hour < 14:
            saludo = "Buenos días"
        elif 14 <= hour < 21:
            saludo = "Buenas tardes"
        else:
            saludo = "Buenas noches"
        t = self.cfg.honorific
        return [
            Spoken(f"{saludo}, {t}. A sus órdenes.", "alegre"),
            Spoken(f"¡{saludo}, {t}! ¿En qué le ayudo?", "alegre"),
            Spoken(f"Aquí estoy, {t}. ¿Qué necesita?", "alegre"),
            Spoken(f"{saludo}, {t}. Sistemas en línea, ¿qué hacemos?", "alegre"),
        ]

    def _greeting(self) -> Spoken:
        return random.choice(self._greetings())

    def _background_phrase(self) -> Spoken:
        return Spoken(
            f"Me quedo en segundo plano, {self.cfg.honorific}. Dos palmadas o "
            "«Jarvis, enciéndete» y vuelvo.",
            "sereno",
        )

    def _prefetch_phrases(self) -> None:
        """Have the fixed phrases ready so waking up and "hecho" are instant."""
        phrases = [Spoken(f"Hecho, {self.cfg.honorific}.", "alegre")]
        phrases.append(self._background_phrase())
        for hour in (9, 16, 22):
            phrases += self._greetings(hour)
        try:
            self.speaker.prefetch(phrases)
        except Exception as exc:  # noqa: BLE001
            logger.info("No se pudieron preparar las frases: %s", exc)

    def _wait_reply(self, timeout: float) -> Optional[str]:
        """Block until the user answers a yes/no question (or *timeout*)."""
        while not self._replies.empty():
            self._replies.get_nowait()  # drop stale answers
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    return None
                try:
                    return self._replies.get(timeout=0.2)
                except queue.Empty:
                    continue
            return None
        finally:
            self._awaiting = False
            self.hub.publish("confirm", pregunta="")
            if self.listener is not None:
                self.listener.pause()
            self._set_state("pensando", "")

    def _remote_audio(self, mp3: bytes) -> None:
        """Send Jarvis' voice to remote devices (the Chromebook) too."""
        if self.hub.remote_clients:
            import base64

            self.hub.publish("audio", mp3=base64.b64encode(mp3).decode("ascii"))

    def _camera_frame(self) -> Optional[str]:
        self.hub.publish("camera", on=True)
        return self.frames.request(timeout=8.0)

    def _hide_for_screen(self) -> None:
        """Get the HUD out of the way while Jarvis looks at / uses the screen."""
        self._hid_for_screen = window.is_visible() and window.minimize()

    def _show_after_screen(self) -> None:
        if self._hid_for_screen and not self._background:
            window.bring_to_front()
        self._hid_for_screen = False

    # ── background mode: hidden, woken by claps or "Jarvis, enciéndete" ──

    def enter_background(self, announce: bool = True) -> None:
        if self._background:
            return
        self.interrupt()
        if announce and self._begin():
            try:
                self._say(self._background_phrase())
            finally:
                self._end()
        self._background = True
        self.hub.publish("background", on=True)
        window.hide()
        self._start_claps()
        if self.listener is not None and self._mic_enabled:
            self.listener.resume()  # the page listens for the wake phrase
        self._set_state("reposo", self._idle_hint())

    def _show_window(self) -> None:
        if not window.bring_to_front() and self.cfg.abrir_ventana:
            from openjarvis.hud.launcher import open_window

            open_window(self.server.url)  # it was closed with the X

    def exit_background(self, source: str = "interfaz") -> None:
        if not self._background:
            self._show_window()
            return
        self._background = False
        if self._claps is not None:
            self._claps.pause()
        self.hub.publish("background", on=False)
        self._show_window()
        self.hub.publish("wake", source=source)
        self.interrupt()
        self.wake(source)

    def _start_claps(self) -> None:
        if not (self._use_mic and self.cfg.palmadas):
            return
        if self._claps is not None:
            self._claps.resume()
            return
        try:
            from openjarvis.hud.triggers import ClapDetector, MicListener

            self._claps = MicListener(
                on_wake=lambda _src: self.exit_background("palmadas"),
                on_level=lambda _lvl: None,
                clap=ClapDetector(self.cfg.sensibilidad_palmadas),
                wake=None,
            )
            self._claps.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Detector de palmadas no disponible: %s", exc)
            self._claps = None

    def _api_background(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("cerrada"):
            # The window was closed with the X: keep running, claps still work.
            # (A reload also fires this; then the page reconnects at once.)
            def _closed() -> None:
                time.sleep(3.0)
                if self.hub.local_clients == 0:
                    self.enter_background(announce=False)

            threading.Thread(target=_closed, daemon=True).start()
        elif payload.get("on", True):
            threading.Thread(target=self.enter_background, daemon=True).start()
        else:
            threading.Thread(target=self.exit_background, daemon=True).start()
        return {"ok": True}

    def _api_wake(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        threading.Thread(target=self.exit_background, daemon=True).start()
        return {"ok": True}

    def _api_quit(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        def _bye() -> None:
            self.hub.publish("quit")
            time.sleep(0.6)  # let the page close its window
            self.stop()

        threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True}

    # ── settings, memory, history, updates ──────────────────────────────

    def _api_config(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        from openjarvis.hud.launcher import autostart_installed, local_addresses

        settings = [
            {**spec, "valor": getattr(self.cfg, spec["attr"])} for spec in SETTINGS
        ]
        return {
            "ajustes": settings,
            "voces": voice_catalog(),
            "voz": self._voice_info(),
            "autoarranque": autostart_installed(),
            "remoto": {
                "activo": self.server.remote,
                "direcciones": [
                    f"http://{ip}:{self.cfg.puerto}/" for ip in local_addresses()
                ],
            },
            "version": _version(),
        }

    def _api_set_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        section, key = str(payload.get("seccion", "")), str(payload.get("clave", ""))
        spec = next(
            (s for s in SETTINGS if s["seccion"] == section and s["clave"] == key),
            None,
        )
        if spec is None:
            raise ValueError("Ajuste desconocido")
        value = coerce_setting(spec, payload.get("valor"))
        if spec["attr"] == "pin" and value and (not value.isdigit() or len(value) < 4):
            raise ValueError("El PIN debe tener al menos 4 cifras.")
        setattr(self.cfg, spec["attr"], value)
        if self.cfg.path is not None:
            save_config_value(self.cfg.path, section, key, value)
        if spec["attr"] == "modelo":
            self.brain._model = None  # noqa: SLF001 - resolve again
        if spec["attr"] == "silencio":
            self.hub.publish("status", silencio=value)
        if spec["attr"] == "pin":
            self.server.pin = value
        return {"valor": value, "reinicio": bool(spec.get("reinicio"))}

    def _api_memory(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"items": self.memory.list()}

    def _publish_memory(self) -> Dict[str, Any]:
        items = self.memory.list()
        self.hub.publish("memory", items=items)
        return {"items": items}

    def _api_memory_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.memory.add(str(payload.get("texto", "")))
        return self._publish_memory()

    def _api_memory_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.memory.delete(str(payload.get("id", "")))
        return self._publish_memory()

    def _api_memory_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.memory.update(str(payload.get("id", "")), str(payload.get("texto", "")))
        return self._publish_memory()

    def _api_memory_import(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("texto", "")).strip()
        if len(text) < 10:
            raise ValueError("Pega el texto que te ha dado la otra IA.")
        added = self.agent.import_memories(text)
        return {"nuevos": added, **self._publish_memory()}

    def _api_history(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "items": [
                {"role": r, "text": c}
                for r, c in self.brain._history[-80:]  # noqa: SLF001
            ]
        }

    def _api_autostart(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        from openjarvis.hud.launcher import install_autostart, remove_autostart

        if payload.get("on"):
            install_autostart()
        else:
            remove_autostart()
        return {"on": bool(payload.get("on"))}

    def _api_update(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        """Download the latest Jarvis and restart (no terminal needed)."""
        from openjarvis.hud.launcher import schedule_update_and_restart

        def _run() -> None:
            self.hub.publish(
                "notice", text="Actualizando Jarvis… vuelvo en un momento."
            )
            time.sleep(0.5)
            schedule_update_and_restart()
            self.hub.publish("quit")
            time.sleep(0.6)
            self.stop()

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    # ── events ─────────────────────────────────────────────────────────

    def _set_state(self, state: str, label: str) -> None:
        self._state = state
        self.hub.publish("state", state=state, label=label)

    def _set_mood(self, mood: str) -> None:
        if mood != self._mood:
            self._mood = mood
            self.hub.publish("mood", mood=mood)

    def _voice_level(self, level: float) -> None:
        self.hub.publish("level", source="voz", value=round(level, 3))

    def _mic_level(self, level: float) -> None:
        self.hub.publish("level", source="mic", value=round(level, 3))


__all__ = ["JarvisAssistant", "voice_catalog"]
