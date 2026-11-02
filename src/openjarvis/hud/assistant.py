"""The always-on Jarvis: wake → briefing → hands-free conversation → sleep."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from openjarvis.hud.brain import (
    JarvisBrain,
    Spoken,
    is_cloud_model,
    is_sleep_command,
    offline_briefing,
)
from openjarvis.hud.config import HudConfig, save_config_value
from openjarvis.hud.context import Snapshot, collect_snapshot
from openjarvis.hud.server import EventHub, HudServer
from openjarvis.hud.skills import NotesStore, TimerManager, parse_note, parse_timer
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
            },
            port=cfg.puerto,
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
        self.brain.cancel_event = self._cancel

    # ── public ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self.server.start()
        self._set_state("arrancando", "Iniciando sistemas…")
        self.hub.publish("voices", items=voice_catalog(), actual=self._voice_info())
        self.hub.publish("notes", items=self.notes.list())
        self.hub.publish("timers", items=self.timers.list())
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

    def _start_listener(self) -> None:
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
        }
        try:
            self.ear.warm_up()
            status["oido"] = self.ear.description if self.ear.ready else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reconocimiento de voz: %s", exc)
            status["oido"] = False
        try:
            self.speaker._resolve()  # noqa: SLF001 - pre-load the TTS backend
            status["voz"] = self.speaker.backend_name
        except Exception:  # noqa: BLE001
            pass
        try:
            model = self.brain.resolve_model()
            self._set_state("arrancando", "Cargando el cerebro en memoria…")
            t0 = time.monotonic()
            self.brain.warm_up()
            logger.info("Modelo %s listo en %.1f s", model, time.monotonic() - t0)
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

    def _idle_hint(self) -> str:
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
                self._get_snapshot()
                self._conversation(first_turn=True)
                return
            self._set_state("activando", "Sistemas en línea")
            snap = self._get_snapshot()
            self._set_state("pensando", "Preparando su briefing…")
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

    def _on_utterance(self, audio: Any) -> None:
        """Continuous mode: you finished a sentence → transcribe and answer."""

        def _handle() -> None:
            if not self._begin():
                return
            try:
                self._set_state("pensando", "Entendiendo…")
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
                self._get_snapshot()
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
        self._set_state("pensando", "Procesando…")
        self._say_stream(
            self.brain.stream_reply(text),
            fallback=lambda: (
                "Lo siento, mi motor de lenguaje no responde en este momento."
            ),
        )

    def _try_skill(self, text: str) -> bool:
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

        def produce() -> None:
            try:
                for sentence in sentences:
                    if self._cancel.is_set():
                        break
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
                audio_q.put((item, self.speaker.synthesize(item)))

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
            sentence, audio = item
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
        if model and is_cloud_model(model):
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
            self._get_snapshot()
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

    def _api_forget(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self.brain.forget()
        self.hub.publish("notice", text="Memoria de conversación borrada.")
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
