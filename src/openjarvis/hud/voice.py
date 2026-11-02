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


WHISPER_PROMPT = (
    "Conversación en español con Jarvis, un asistente personal. "
    "Hola Jarvis, ¿qué tengo hoy en la agenda? ¿Cuántos leads han entrado?"
)


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def choose_whisper_model(requested: str, cuda: bool) -> tuple[str, str, str]:
    """Return ``(model, device, compute_type)`` for faster-whisper.

    ``auto`` picks the most accurate model the hardware can run in real time:
    large-v3-turbo on an NVIDIA GPU, medium (int8) on CPU.
    """
    req = (requested or "auto").strip()
    device = "cuda" if cuda else "cpu"
    compute = "float16" if cuda else "int8"
    if req == "auto":
        return ("large-v3-turbo" if cuda else "medium"), device, compute
    return req, device, compute


class AdaptiveRecorder:
    """Record one utterance, calibrating the silence threshold to the room.

    The first ~0.3 s measure background noise; speech starts when the level
    rises well above it and ends after a short pause. This works with quiet
    microphones and noisy rooms where a fixed threshold either never triggers
    or cuts sentences in half.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        end_silence: float = 1.0,
        startup_timeout: float = 6.0,
        max_seconds: float = 25.0,
        on_level: Optional[LevelFn] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.end_silence = end_silence
        self.startup_timeout = startup_timeout
        self.max_seconds = max_seconds
        self.on_level = on_level

    def record(self) -> "Any":
        import numpy as np
        import sounddevice as sd

        block = int(self.sample_rate * 0.03)  # 30 ms
        calib_blocks = 10
        frames: list = []
        rms_hist: list[float] = []
        speech = False
        quiet = 0
        pre_roll = 10  # keep ~300 ms before speech starts
        threshold = 0.0

        with sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32", blocksize=block
        ) as stream:
            total = int(self.max_seconds / 0.03)
            for i in range(total):
                data, _ = stream.read(block)
                chunk = np.asarray(data, dtype=np.float32).reshape(-1)
                rms = float(np.sqrt(np.mean(chunk * chunk)) + 1e-7)
                if self.on_level is not None:
                    self.on_level(min(1.0, rms * 12))
                frames.append(chunk)
                if i < calib_blocks:
                    rms_hist.append(rms)
                    continue
                if not threshold:
                    ambient = float(np.median(rms_hist))
                    threshold = max(0.008, ambient * 3.2)
                if rms > threshold:
                    if not speech:
                        frames = frames[-pre_roll:]
                    speech = True
                    quiet = 0
                elif speech:
                    quiet += 1
                    if quiet * 0.03 >= self.end_silence:
                        break
                elif i * 0.03 >= self.startup_timeout:
                    return np.zeros(0, dtype=np.float32)
                if not speech and len(frames) > pre_roll:
                    frames = frames[-pre_roll:]
        if not speech:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames)
        peak = float(np.max(np.abs(audio))) or 1.0
        return (audio / peak * 0.9).astype(np.float32)  # normalise quiet mics


class Ear:
    """Record one utterance and transcribe it (Whisper, Spanish-tuned)."""

    def __init__(self, cfg: HudConfig, on_level: Optional[LevelFn] = None) -> None:
        self.cfg = cfg
        self.on_level = on_level
        self._model: Any = None
        self._cloud: Any = None
        self._resolved = False
        self.description = ""

    def _resolve(self) -> bool:
        if self._resolved:
            return self._model is not None or self._cloud is not None
        self._resolved = True
        if self.cfg.motor_oido == "openai":
            from openjarvis.core.config import load_config
            from openjarvis.speech._discovery import _create_backend

            self._cloud = _create_backend("openai", load_config())
            if self._cloud is not None:
                self.description = "OpenAI (nube)"
                return True
            logger.warning("Falta OPENAI_API_KEY; uso Whisper local")
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.warning("faster-whisper no instalado (uv sync --extra desktop)")
            return False
        name, device, compute = choose_whisper_model(
            self.cfg.modelo_oido, _cuda_available()
        )
        try:
            self._model = WhisperModel(name, device=device, compute_type=compute)
        except Exception as exc:  # noqa: BLE001 - e.g. CUDA libs missing
            logger.warning("Whisper %s en %s falló (%s); uso CPU", name, device, exc)
            name, device, compute = choose_whisper_model(self.cfg.modelo_oido, False)
            self._model = WhisperModel(name, device=device, compute_type=compute)
        self.description = f"Whisper {name} ({'GPU' if device == 'cuda' else 'CPU'})"
        return True

    @property
    def ready(self) -> bool:
        return self._resolve()

    def warm_up(self) -> None:
        """Load the speech model in advance so the first reply is fast."""
        self._resolve()

    def listen(self, startup_silence: float = 6.0) -> str:
        if not self._resolve():
            raise RuntimeError(
                "No hay reconocimiento de voz disponible. "
                "Instala las dependencias con: uv sync --extra desktop --extra hud"
            )
        audio = AdaptiveRecorder(
            startup_timeout=startup_silence, on_level=self.on_level
        ).record()
        if len(audio) < 16000 * 0.4:  # < 0.4 s → nothing said
            return ""
        return self.transcribe(audio)

    def transcribe(self, audio: "Any") -> str:
        lang = self.cfg.idioma_escucha or None
        if self._cloud is not None:
            import io

            import soundfile as sf

            buf = io.BytesIO()
            sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
            result = self._cloud.transcribe(buf.getvalue(), format="wav", language=lang)
            return (result.text or "").strip()
        segments, _info = self._model.transcribe(
            audio,
            language=lang,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=WHISPER_PROMPT,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # Whisper sometimes hallucinates the prompt or stock phrases on silence.
        junk = ("subtítulos", "gracias por ver", "amara.org", WHISPER_PROMPT[:30])
        if any(j.lower() in text.lower() for j in junk) and len(text) < 80:
            return ""
        return text


__all__ = ["AdaptiveRecorder", "Ear", "Speaker", "choose_whisper_model"]
