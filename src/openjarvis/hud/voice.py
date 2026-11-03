"""Speak and listen for the HUD, reporting audio levels for the animation."""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from openjarvis.hud.config import HudConfig

logger = logging.getLogger(__name__)

LevelFn = Callable[[float], None]


# How each emotion colours the voice: (speed multiplier, pitch Hz, volume %).
# Small shifts on purpose — enough to hear the mood, never cartoonish.
# Slightly upbeat even when neutral: flat delivery is what sounds robotic.
MOOD_PROSODY = {
    "neutral": (1.02, 2, 0),
    "alegre": (1.07, 7, 4),
    "entusiasta": (1.12, 12, 10),
    "bromista": (1.06, 6, 4),
    "sereno": (0.98, 0, -2),
    "serio": (0.96, -4, 0),
    "empatico": (0.94, -2, -6),
    "sorprendido": (1.09, 13, 6),
}

# ElevenLabs v3 audio tags for each mood (only used with an eleven_v3 model).
MOOD_ELEVEN_TAGS = {
    "alegre": "[happy]",
    "entusiasta": "[excited]",
    "bromista": "[playful]",
    "sereno": "[calm]",
    "serio": "[serious]",
    "empatico": "[warmly]",
    "sorprendido": "[surprised]",
}


def prosody_for(
    mood: str, base_speed: float = 1.0, base_pitch: int = 0
) -> tuple[float, str, str]:
    """Return ``(speed, pitch, volume)`` for edge-tts given an emotion."""
    speed, pitch, volume = MOOD_PROSODY.get(mood or "neutral", MOOD_PROSODY["neutral"])
    return (
        round(base_speed * speed, 3),
        f"{pitch + int(base_pitch):+d}Hz",
        f"{volume:+d}%",
    )


class AudioStream:
    """Speech that can start playing while it is still being synthesised."""

    def __init__(self) -> None:
        import queue

        self._q: "queue.Queue[Any]" = queue.Queue()
        self.ready = threading.Event()  # first audio (or the end) arrived
        self.sample_rate = 24000

    def put(self, pcm: Any, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._q.put(pcm)
        self.ready.set()

    def close(self) -> None:
        self._q.put(None)
        self.ready.set()

    def __iter__(self) -> Any:
        while (pcm := self._q.get()) is not None:
            yield pcm


class _Mp3Decoder:
    """Incremental MP3 to float32 PCM (PyAV), for audio arriving in chunks."""

    def __init__(self) -> None:
        import av

        self._codec = av.CodecContext.create("mp3", "r")

    def _pcm(self, frames: Any) -> list:
        import numpy as np

        out = []
        for frame in frames:
            pcm = frame.to_ndarray()
            if pcm.dtype.kind == "i":
                pcm = pcm / 32768.0
            if pcm.ndim > 1 and frame.format.is_planar:
                pcm = pcm.mean(axis=0)  # planar: one row per channel
            pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
            out.append((pcm, frame.sample_rate))
        return out

    def feed(self, data: Optional[bytes]) -> list:
        """Decode a chunk; ``None`` flushes what the parser still holds."""
        out = []
        for packet in self._codec.parse(data):
            try:
                out += self._pcm(self._codec.decode(packet))
            except Exception:  # noqa: BLE001 - e.g. an ID3 tag: skip it
                continue
        return out

    def flush(self) -> list:
        return self.feed(None) + self._pcm(self._codec.decode(None))


class Speaker:
    """Text-to-speech with the configured backend and graceful fallback."""

    def __init__(self, cfg: HudConfig) -> None:
        self.cfg = cfg
        self._backend: Any = None
        self._attempted: set[str] = set()
        self._stop = threading.Event()
        # Called with each sentence's MP3 (edge-tts): lets a remote device
        # (the Chromebook) play Jarvis' voice too.
        self.on_mp3: Optional[Callable[[bytes], None]] = None
        # Fixed phrases (greetings, "Hecho, señor") synthesised in advance,
        # so they play instantly: {(text, mood): ([pcm chunks], rate, mp3)}.
        self._cache: Dict[Tuple[str, str], Tuple[list, int, bytes]] = {}

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

    def set_voice(self, motor: str = "", voz: str = "") -> None:
        """Switch voice at runtime (from the HUD voice picker)."""
        if motor and motor != self.cfg.motor_voz:
            self.cfg.motor_voz = motor
            self._backend = None
            self._attempted.clear()
        if voz:
            self.cfg.voz = voz
        self._cache.clear()  # prepared phrases were in the old voice

    def synthesize(self, text: str, mood: str = "") -> Optional[bytes]:
        mood = mood or getattr(text, "mood", "") or "neutral"
        while (backend := self._resolve()) is not None:
            backend_id = getattr(backend, "backend_id", "")
            voices = [self._voice_for(backend)]
            if backend_id == "edge":
                from openjarvis.speech.edge_tts_backend import DEFAULT_VOICE

                # Microsoft's service now and then sends no audio for a
                # sentence: retry the same voice, then use the default voice
                # for this sentence only (the chosen voice is kept).
                voices = [voices[0], voices[0], DEFAULT_VOICE]
            for voice in voices:
                try:
                    kwargs: dict = {
                        "output_format": "wav",
                        "speed": self.cfg.velocidad,
                    }
                    if voice:
                        kwargs["voice_id"] = voice
                    say = str(text)
                    if backend_id == "edge":
                        speed, pitch, volume = prosody_for(
                            mood, self.cfg.velocidad, self.cfg.tono
                        )
                        kwargs.update(speed=speed, pitch=pitch, volume=volume)
                    elif backend_id == "elevenlabs":
                        model = getattr(backend, "_model", "") or ""
                        tag = MOOD_ELEVEN_TAGS.get(mood)
                        if tag and "v3" in model:
                            say = f"{tag} {say}"
                    result = backend.synthesize(say, **kwargs)
                    if result.audio:
                        if self.on_mp3 is not None and result.format == "mp3":
                            self.on_mp3(result.audio)
                        return result.audio
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Voz %r falló: %s", voice or backend_id, exc)
            self._backend = None
        logger.warning("No hay motor de voz disponible (instala: pip install edge-tts)")
        return None

    def warm_up(self) -> None:
        """Load the voice engine and audio libraries before the first reply.

        Importing edge-tts and opening its first connection took ~10 s on a
        laptop; done at start-up, the first answer no longer waits for it.
        """
        import sounddevice  # noqa: F401
        import soundfile  # noqa: F401

        audio = self.synthesize_streaming("Listo.")
        if isinstance(audio, AudioStream):
            for _ in audio:  # wait for it to arrive, without playing it
                pass

    def synthesize_streaming(self, text: str, mood: str = "") -> Any:
        """Start synthesising *text* and return an :class:`AudioStream` at once.

        edge-tts sends the MP3 in chunks: decoding them as they arrive lets
        Jarvis start talking ~0.4-1 s sooner than waiting for the whole
        sentence. Other voice engines (or a missing PyAV) fall back to
        :meth:`synthesize` and return the complete audio as bytes.
        """
        key = (str(text), mood or getattr(text, "mood", "") or "neutral")
        cached = self._cache.get(key)
        if cached is not None:
            chunks, rate, mp3 = cached
            ready = AudioStream()
            for pcm in chunks:
                ready.put(pcm, rate)
            ready.close()
            if self.on_mp3 is not None and mp3:
                self.on_mp3(mp3)
            return ready
        backend = self._resolve()
        if getattr(backend, "backend_id", "") != "edge":
            return self.synthesize(text, mood)
        try:
            decoder = _Mp3Decoder()
        except Exception:  # noqa: BLE001 - PyAV not installed
            return self.synthesize(text, mood)

        mood = mood or getattr(text, "mood", "") or "neutral"
        speed, pitch, volume = prosody_for(mood, self.cfg.velocidad, self.cfg.tono)
        voice = self._voice_for(backend)
        stream = AudioStream()

        async def _pump() -> None:
            import edge_tts

            from openjarvis.speech.edge_tts_backend import (
                DEFAULT_VOICE,
                _rate_from_speed,
            )

            communicate = edge_tts.Communicate(
                str(text),
                voice or DEFAULT_VOICE,
                rate=_rate_from_speed(speed),
                pitch=pitch,
                volume=volume,
            )
            mp3: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    mp3.append(chunk["data"])
                    for pcm, rate in decoder.feed(chunk["data"]):
                        stream.put(pcm, rate)
            for pcm, rate in decoder.flush():
                stream.put(pcm, rate)
            if self.on_mp3 is not None and mp3:
                self.on_mp3(b"".join(mp3))

        def _run() -> None:
            import asyncio

            try:
                asyncio.run(_pump())
            except Exception as exc:  # noqa: BLE001
                if stream.ready.is_set():
                    logger.warning("La voz se cortó: %s", exc)
                else:
                    # Nothing played yet: use the normal path, which retries
                    # with the default voice and other engines.
                    self._put_bytes(stream, self.synthesize(text, mood))
            finally:
                stream.close()

        threading.Thread(target=_run, name="jarvis-tts-stream", daemon=True).start()
        return stream

    @staticmethod
    def _put_bytes(stream: AudioStream, audio: Optional[bytes]) -> None:
        if not audio:
            return
        import soundfile as sf

        data, sr = sf.read(io.BytesIO(audio), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        stream.put(data, sr)

    def prefetch(self, phrases: list) -> None:
        """Synthesise fixed phrases now so they play with no delay later."""
        hook, self.on_mp3 = self.on_mp3, None
        try:
            for text in phrases:
                mood = getattr(text, "mood", "") or "neutral"
                key = (str(text), mood)
                if key in self._cache:
                    continue
                mp3: list = []
                self.on_mp3 = mp3.append
                stream = self.synthesize_streaming(text, mood)
                if isinstance(stream, AudioStream):
                    chunks = list(stream)
                    if chunks:
                        self._cache[key] = (chunks, stream.sample_rate, b"".join(mp3))
                self.on_mp3 = None
        finally:
            self.on_mp3 = hook

    def clear_cache(self) -> None:
        self._cache.clear()

    def _play_stream(self, stream: AudioStream, on_level: Optional[LevelFn]) -> None:
        """Play an :class:`AudioStream` chunk by chunk as it arrives."""
        import numpy as np
        import sounddevice as sd

        self._stop.clear()
        out = None
        peak = 0.05
        try:
            for pcm in stream:
                if self._stop.is_set():
                    break
                if out is None:
                    out = sd.OutputStream(
                        samplerate=stream.sample_rate, channels=1, dtype="float32"
                    )
                    out.start()
                hop = max(1, int(stream.sample_rate * 0.05))
                for i in range(0, len(pcm), hop):
                    if self._stop.is_set():
                        break
                    block = pcm[i : i + hop]
                    if on_level is not None and len(block):
                        rms = float(np.sqrt(np.mean(block**2)))
                        peak = max(peak * 0.995, rms)
                        on_level(min(1.0, rms / peak))
                    out.write(block.reshape(-1, 1))  # blocks: keeps real time
        finally:
            if out is not None:
                if self._stop.is_set():
                    out.abort()
                else:
                    out.stop()  # let the last buffer play out
                out.close()
            if on_level is not None:
                on_level(0.0)

    def stop(self) -> None:
        self._stop.set()

    def play(self, audio: Any, on_level: Optional[LevelFn] = None) -> None:
        """Play audio, streaming a 0–1 loudness envelope to *on_level*."""
        if isinstance(audio, AudioStream):
            self._play_stream(audio, on_level)
            return
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

    def say(
        self, text: str, on_level: Optional[LevelFn] = None, mood: str = ""
    ) -> bool:
        audio = self.synthesize_streaming(text, mood)
        if not audio:
            return False
        try:
            self.play(audio, on_level)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo reproducir el audio: %s", exc)
            return False


# Whisper hallucinates these on silence or unclear audio. (An earlier version
# passed example questions as the initial prompt; Whisper then "heard" those
# same questions whenever the audio was poor, so Jarvis always answered the
# same thing. Only the name is hinted now, via hotwords.)
HALLUCINATIONS = (
    "subtítulos",
    "subtitulos",
    "amara.org",
    "gracias por ver",
    "suscríbete",
    "suscribete",
    "gracias por su atención",
    "música",
    "¡gracias!",
)


def is_hallucination(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in ("gracias.", "gracias", "¡gracias.", "...", "."):
        return True
    return len(t) < 80 and any(h in t for h in HALLUCINATIONS)


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def _cpu_threads() -> int:
    """Physical cores: hyper-threads slow Whisper down on laptops."""
    try:
        import psutil

        return max(1, psutil.cpu_count(logical=False) or 4)
    except Exception:  # noqa: BLE001
        return 4


def _ram_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / 1024**3
    except Exception:  # noqa: BLE001
        return 16.0


def choose_whisper_model(
    requested: str, cuda: bool, ram_gb: float = 16.0
) -> tuple[str, str, str]:
    """Return ``(model, device, compute_type)`` for faster-whisper.

    ``auto`` picks the most accurate model the hardware can run in real time:
    large-v3-turbo on an NVIDIA GPU, medium (int8) on a CPU with plenty of RAM,
    small on machines with less than 12 GB — where the language model needs
    the memory more.
    """
    req = (requested or "auto").strip()
    device = "cuda" if cuda else "cpu"
    compute = "float16" if cuda else "int8"
    if req == "auto":
        if cuda:
            return "large-v3-turbo", device, compute
        return ("medium" if ram_gb >= 12 else "small"), device, compute
    return req, device, compute


class EndpointDetector:
    """Decide, block by block, when an utterance starts and ends.

    Works on per-block RMS values (30 ms blocks). Speech starts when the level
    clearly exceeds the calibrated room noise. It ends after ``end_silence``
    seconds below a threshold *relative to how loud the speaker was* — laptop
    microphones with automatic gain raise the background after you stop
    talking, so a fixed noise threshold would keep recording until the cap.
    """

    BLOCK = 0.03

    def __init__(
        self,
        *,
        end_silence: float = 1.0,
        startup_timeout: float = 6.0,
        max_utterance: float = 15.0,
        calib_blocks: int = 10,
    ) -> None:
        self.end_silence = end_silence
        self.startup_timeout = startup_timeout
        self.max_utterance = max_utterance
        self.calib_blocks = calib_blocks
        self._calib: list[float] = []
        self.ambient = 0.0
        self.speech = False
        self._peak = 0.0
        self._quiet = 0
        self._n = 0
        self._speech_blocks = 0

    @property
    def start_threshold(self) -> float:
        return max(0.008, self.ambient * 3.0)

    def feed(self, rms: float) -> str:
        """Return ``"wait"``, ``"speech"``, ``"end"`` or ``"timeout"``."""
        self._n += 1
        if self._n <= self.calib_blocks:
            self._calib.append(rms)
            if self._n < self.calib_blocks:
                return "wait"
            # People start talking the moment they press the mic button, so
            # the calibration window often contains speech. The *quietest*
            # block (gaps between syllables) is a far better noise estimate
            # than the median, and is capped so speech can never become the
            # "noise floor".
            self.ambient = min(min(self._calib) * 1.3, 0.02)
            loud = [r for r in self._calib if r > self.start_threshold]
            if len(loud) >= 2:
                # Speech already began during calibration: keep it.
                self.speech = True
                self._peak = max(self._calib)
                self._speech_blocks = len(self._calib)
                return "speech"
            return "wait"

        if not self.speech:
            if rms > self.start_threshold:
                self.speech = True
                self._peak = rms
                self._speech_blocks = 1
                return "speech"
            self.ambient = 0.95 * self.ambient + 0.05 * rms
            if self._n * self.BLOCK >= self.startup_timeout:
                return "timeout"
            return "wait"

        self._speech_blocks += 1
        self._peak = max(self._peak * 0.997, rms)
        silence_level = max(self.start_threshold * 0.8, self._peak * 0.2)
        if rms < silence_level:
            self._quiet += 1
        else:
            self._quiet = 0
        if self._quiet * self.BLOCK >= self.end_silence:
            return "end"
        if self._speech_blocks * self.BLOCK >= self.max_utterance:
            return "end"
        return "speech"


class AdaptiveRecorder:
    """Record one utterance from the default microphone (see EndpointDetector)."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        end_silence: float = 1.0,
        startup_timeout: float = 6.0,
        max_utterance: float = 15.0,
        on_level: Optional[LevelFn] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.detector = EndpointDetector(
            end_silence=end_silence,
            startup_timeout=startup_timeout,
            max_utterance=max_utterance,
        )
        self.on_level = on_level

    def record(self, should_stop: Optional[Callable[[], bool]] = None) -> "Any":
        import numpy as np
        import sounddevice as sd

        block = int(self.sample_rate * EndpointDetector.BLOCK)
        pre_roll = 10  # keep ~300 ms before speech starts
        frames: list = []
        det = self.detector
        hard_cap = int((det.startup_timeout + det.max_utterance + 2) / det.BLOCK)

        with sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32", blocksize=block
        ) as stream:
            for _ in range(hard_cap):
                if should_stop is not None and should_stop():
                    return np.zeros(0, dtype=np.float32)
                data, _ = stream.read(block)
                chunk = np.asarray(data, dtype=np.float32).reshape(-1)
                rms = float(np.sqrt(np.mean(chunk * chunk)) + 1e-7)
                if self.on_level is not None:
                    self.on_level(min(1.0, rms * 12))
                frames.append(chunk)
                verdict = det.feed(rms)
                if verdict == "timeout":
                    return np.zeros(0, dtype=np.float32)
                if verdict == "end":
                    break
                if not det.speech and len(frames) > pre_roll:
                    frames = frames[-pre_roll:]
        if not det.speech:
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
        self.last_timing: dict = {}

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
            self.cfg.modelo_oido, _cuda_available(), _ram_gb()
        )
        try:
            self._model = WhisperModel(
                name, device=device, compute_type=compute, cpu_threads=_cpu_threads()
            )
        except Exception as exc:  # noqa: BLE001 - e.g. CUDA libs missing
            logger.warning("Whisper %s en %s falló (%s); uso CPU", name, device, exc)
            name, device, compute = choose_whisper_model(
                self.cfg.modelo_oido, False, _ram_gb()
            )
            self._model = WhisperModel(name, device=device, compute_type=compute)
        self.description = f"Whisper {name} ({'GPU' if device == 'cuda' else 'CPU'})"
        self._beam = 5 if device == "cuda" else 1  # CPU: greedy decoding is fastest
        return True

    @property
    def ready(self) -> bool:
        return self._resolve()

    def warm_up(self) -> None:
        """Load the speech model in advance so the first reply is fast."""
        self._resolve()

    def listen(
        self,
        startup_silence: float = 6.0,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> str:
        if not self._resolve():
            raise RuntimeError(
                "No hay reconocimiento de voz disponible. "
                "Instala las dependencias con: uv sync --extra desktop --extra hud"
            )
        t0 = time.monotonic()
        audio = AdaptiveRecorder(
            startup_timeout=startup_silence, on_level=self.on_level
        ).record(should_stop)
        t1 = time.monotonic()
        self.last_timing = {"grabacion": t1 - t0, "audio": len(audio) / 16000}
        if len(audio) < 16000 * 0.4:  # < 0.4 s → nothing said
            return ""
        text = self.transcribe(audio)
        self.last_timing["transcripcion"] = time.monotonic() - t1
        return text

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
            beam_size=getattr(self, "_beam", 2),
            vad_filter=True,
            condition_on_previous_text=False,
            hotwords="Jarvis",
            without_timestamps=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return "" if is_hallucination(text) else text


__all__ = [
    "AdaptiveRecorder",
    "EndpointDetector",
    "Ear",
    "Speaker",
    "choose_whisper_model",
]
