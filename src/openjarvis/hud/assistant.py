"""The always-on Jarvis: wake → briefing → hands-free conversation → sleep."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from openjarvis.hud.brain import JarvisBrain, is_sleep_command, offline_briefing
from openjarvis.hud.config import HudConfig
from openjarvis.hud.context import Snapshot, collect_snapshot
from openjarvis.hud.server import EventHub, HudServer
from openjarvis.hud.voice import Ear, Speaker

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_AGE = 300  # seconds before the data is refreshed on wake
SNAPSHOT_REFRESH = 900  # background refresh period


class JarvisAssistant:
    def __init__(self, cfg: HudConfig, *, mic: bool = True) -> None:
        self.cfg = cfg
        self.hub = EventHub()
        self.brain = JarvisBrain(cfg)
        self.speaker = Speaker(cfg)
        self.ear = Ear(cfg, on_level=self._mic_level)
        self.server = HudServer(
            self.hub,
            {
                "activate": self._api_activate,
                "ask": self._api_ask,
                "stop": self._api_stop,
                "refresh": self._api_refresh,
            },
            port=cfg.puerto,
        )
        self._use_mic = mic
        self.listener: Any = None
        self._session = threading.Lock()
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._snapshot: Optional[Snapshot] = None
        self._snapshot_at = 0.0
        self._snapshot_lock = threading.Lock()
        self._state = ""

    # ── public ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self.server.start()
        self._set_state("arrancando", "Iniciando sistemas…")
        if self._use_mic:
            self._start_listener()
        threading.Thread(
            target=self._warm_up, name="jarvis-warmup", daemon=True
        ).start()
        threading.Thread(
            target=self._refresh_loop, name="jarvis-data", daemon=True
        ).start()

    def stop(self) -> None:
        self._shutdown.set()
        self._cancel.set()
        self.speaker.stop()
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

    def _start_listener(self) -> None:
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
            sdk = self.brain._ensure_sdk()  # noqa: SLF001
            sdk._ensure_engine()  # noqa: SLF001
            engine = getattr(sdk, "_resolved_engine_key", "") or "ok"
            status["motor"] = (
                f"{self.cfg.modelo} ({engine})" if self.cfg.modelo else engine
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Motor de IA no disponible: %s", exc)
            status["motor"] = ""
        self.hub.publish("status", **status)
        if not self._session.locked():
            self._set_state("reposo", self._idle_hint())

    def _idle_hint(self) -> str:
        ways = []
        if self.listener and "palmadas" in self.listener.detectors:
            ways.append("dos palmadas")
        if self.listener and "voz" in self.listener.detectors:
            ways.append("«Hey Jarvis»")
        if not ways:
            return "Pulsa el núcleo para activarme"
        text = " o ".join(ways)
        return text[:1].upper() + text[1:] + " para activarme"

    # ── data ───────────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        while not self._shutdown.is_set():
            self._get_snapshot(force=True)
            self._shutdown.wait(SNAPSHOT_REFRESH)

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

    def _run_session(self, source: str) -> None:
        if not self._session.acquire(blocking=False):
            return
        self._cancel.clear()
        if self.listener is not None:
            self.listener.pause()
        try:
            self.hub.publish("wake", source=source)
            self._set_state("activando", "Sistemas en línea")
            snap = self._get_snapshot()

            self._set_state("pensando", "Preparando su briefing…")
            try:
                text = self.brain.briefing(source)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Briefing sin IA: %s", exc)
                text = offline_briefing(self.cfg, snap)
            self._say(text)
            self._conversation()
        except Exception:  # noqa: BLE001
            logger.exception("Error en la sesión de Jarvis")
            self._set_state("error", "Algo ha fallado — revisa la consola")
            time.sleep(2)
        finally:
            self.hub.publish("transcript", text="", final=True)
            self._set_state("reposo", self._idle_hint())
            if self.listener is not None:
                self.listener.resume()
            self._session.release()

    def _conversation(self) -> None:
        silences = 0
        while not self._cancel.is_set() and not self._shutdown.is_set():
            self._set_state("escuchando", "Le escucho…")
            try:
                heard = self.ear.listen()
            except Exception as exc:  # noqa: BLE001
                logger.warning("No puedo escuchar: %s", exc)
                self.hub.publish("notice", text=str(exc))
                return
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
                self._say(f"A su disposición, {self.cfg.honorific}.")
                return
            self._answer(heard)

    def _answer(self, text: str) -> None:
        self._set_state("pensando", "Procesando…")
        try:
            reply = self.brain.reply(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fallo del motor de IA: %s", exc)
            reply = "Lo siento, mi motor de lenguaje no responde en este momento."
        self._say(reply)

    def _say(self, text: str) -> None:
        self.hub.publish("message", role="jarvis", text=text)
        self._set_state("hablando", "")
        self.speaker.say(text, on_level=self._voice_level)

    # ── HUD API ────────────────────────────────────────────────────────

    def _api_activate(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"started": self.wake("interfaz")}

    def _api_ask(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("text", "")).strip()[:2000]
        if not text:
            return {"started": False}

        def _job() -> None:
            if not self._session.acquire(blocking=False):
                return
            if self.listener is not None:
                self.listener.pause()
            try:
                self._get_snapshot()
                self.hub.publish("message", role="user", text=text)
                self._answer(text)
            finally:
                self._set_state("reposo", self._idle_hint())
                if self.listener is not None:
                    self.listener.resume()
                self._session.release()

        busy = self._session.locked()
        if not busy:
            threading.Thread(target=_job, daemon=True).start()
        return {"started": not busy}

    def _api_stop(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self._cancel.set()
        self.speaker.stop()
        return {"stopped": True}

    def _api_refresh(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        threading.Thread(
            target=self._get_snapshot, kwargs={"force": True}, daemon=True
        ).start()
        return {"refreshing": True}

    # ── events ─────────────────────────────────────────────────────────

    def _set_state(self, state: str, label: str) -> None:
        self._state = state
        self.hub.publish("state", state=state, label=label)

    def _voice_level(self, level: float) -> None:
        self.hub.publish("level", source="voz", value=round(level, 3))

    def _mic_level(self, level: float) -> None:
        self.hub.publish("level", source="mic", value=round(level, 3))


__all__ = ["JarvisAssistant"]
