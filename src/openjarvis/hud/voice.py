"""Speak and listen for the HUD, reporting audio levels for the animation."""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Any, Callable, Optional

from openjarvis.hud.config import HudConfig

logger = logging.getLogger(__name__)

LevelFn = Callable[[float], None]


class Speaker:
    """Text-to-speech with the configured backend and graceful fallback."""

    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self._backend: Any = None
        self._attempted: set[str] = set()
        self._stop = threading.Event()

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "backend_id", "") or "—"

    def _resolve(self) -> Any:
        if self._backend is None:
            from openjarvis.speech._tts_discovery import get_tts_backend

            self._backend = get_tts_backend(
                self.cfg.motor_voz, attempted=self._attempted
            )
        return self._backend

    def _voice_for(self, backend: Any) -> str:
        if getattr(backend, "backend_id", "") == self.cfg.motor_voz:
            return self.cfg.voz
        from openjarvis.speech._tts_discovery import default_voice_for

        return default_voice_for(getattr(backend, "backend_id", ""))

    def synthesize(self, text: str) -> Optional[bytes]:
        while (backend := self._resolve()) is not None:
            try:
                kwargs: dict = {"output_format": "wav", "speed": self.cfg.velocidad}
                voice = self._voice_for(backend)
                if voice:
                    kwargs["voice_id"] = voice
                result = backend.synthesize(text, **kwargs)
                if result.audio:
                    return result.audio
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Voz %r falló: %s", getattr(backend, "backend_id", "?"), exc
                )
            self._backend = None
        logger.warning("No hay motor de voz disponible (instala: pip install edge-tts)")
        return None

    def stop(self) -> None:
        self._stop.set()

    def play(self, audio: bytes, on_level: Optional[LevelFn] = None) -> None:
        """Play audio, streaming a 0–1 loudness envelope to *on_level*."""
        import numpy as np
        import sounddevice as sd
        import soundfile as sf

        data, sr = sf.read(io.BytesIO(audio), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        hop = max(1, int(sr * 0.05))
        env = [
            float(np.sqrt(np.mean(data[i : i + hop] ** 2)))
            for i in range(0, len(data), hop)
        ]
        top = max(env) if env else 1.0
        env = [min(1.0, e / (top or 1.0)) for e in env]

        self._stop.clear()
        sd.play(data, sr)
        start = time.monotonic()
        duration = len(data) / sr
        try:
            while (elapsed := time.monotonic() - start) < duration:
                if self._stop.is_set():
                    sd.stop()
                    break
                if on_level is not None:
                    idx = min(len(env) - 1, int(elapsed / 0.05)) if env else 0
                    on_level(env[idx] if env else 0.0)
                time.sleep(0.04)
            sd.wait()
        finally:
            if on_level is not None:
                on_level(0.0)

    def say(self, text: str, on_level: Optional[LevelFn] = None) -> bool:
        audio = self.synthesize(text)
        if not audio:
            return False
        try:
            self.play(audio, on_level)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo reproducir el audio: %s", exc)
            return False


class Ear:
    """Record one utterance and transcribe it."""

    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self._backend: Any = None
        self._resolved = False

    def _resolve(self) -> Any:
        if not self._resolved:
            from openjarvis.core.config import load_config
            from openjarvis.speech._discovery import get_speech_backend

            config = load_config()
            # A larger Whisper model hears Spanish much better than "base".
            try:
                if config.speech.model in ("tiny", "base"):
                    config.speech.model = "small"
                if config.speech.device == "auto":
                    config.speech.compute_type = "int8"
            except Exception:  # noqa: BLE001
                pass
            self._backend = get_speech_backend(config)
            self._resolved = True
        return self._backend

    @property
    def ready(self) -> bool:
        return self._resolve() is not None

    def warm_up(self) -> None:
        """Load the speech model in advance so the first reply is fast."""
        self._resolve()

    def listen(self, startup_silence: float = 6.0) -> str:
        from openjarvis.speech.voice_io import record_until_silence

        backend = self._resolve()
        if backend is None:
            raise RuntimeError(
                "No hay reconocimiento de voz disponible. "
                "Instala las dependencias con: uv sync --extra desktop --extra hud"
            )
        audio = record_until_silence(
            silence_seconds=1.2, startup_silence_seconds=startup_silence
        )
        if len(audio) < 16000:  # < ~0.5 s of audio → nothing said
            return ""
        result = backend.transcribe(
            audio, format="wav", language=self.cfg.idioma_escucha or None
        )
        return (result.text or "").strip()


__all__ = ["Ear", "Speaker"]
