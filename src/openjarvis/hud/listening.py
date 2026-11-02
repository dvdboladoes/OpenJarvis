"""Always-on listening: talk to Jarvis without pressing anything.

The microphone stays open; :class:`SpeechSegmenter` cuts the stream into
utterances (you start talking → you stop for a moment → one utterance), and
:class:`ContinuousListener` hands each one to the assistant, which transcribes
and answers. While Jarvis is thinking or speaking the listener is paused, so
it never hears its own voice through the laptop speakers.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, List, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.03  # 30 ms analysis blocks


class SpeechSegmenter:
    """Turn a stream of 30 ms blocks into complete utterances.

    * The background noise level is learned continuously (only while nobody
      is speaking), so a fan or street noise does not count as speech.
    * Speech starts after ``start_blocks`` consecutive blocks clearly above
      the noise floor (a cough or a click is too short to count).
    * It ends after ``end_silence`` seconds below a level relative to how
      loud you were speaking — robust to laptop mics with automatic gain.
    * Utterances shorter than ``min_speech`` seconds are discarded.
    """

    def __init__(
        self,
        *,
        end_silence: float = 0.75,
        min_speech: float = 0.45,
        max_utterance: float = 20.0,
        start_blocks: int = 4,
        pre_roll: float = 0.35,
        sensitivity: float = 0.5,
    ) -> None:
        s = max(0.0, min(1.0, float(sensitivity)))
        self.ratio = 4.0 - 2.0 * s  # how far above the noise floor speech must be
        self.floor_min = 0.006
        self.end_blocks = max(1, int(end_silence / BLOCK_SECONDS))
        self.min_blocks = max(1, int(min_speech / BLOCK_SECONDS))
        self.max_blocks = max(1, int(max_utterance / BLOCK_SECONDS))
        self.start_blocks = start_blocks
        self.pre_blocks = max(1, int(pre_roll / BLOCK_SECONDS))
        self.reset()

    def reset(self) -> None:
        self.noise = 0.01
        self._warm = 0
        self._pre: Deque[Any] = deque(maxlen=self.pre_blocks)
        self._candidate: List[Any] = []
        self._frames: List[Any] = []
        self._speaking = False
        self._voiced = 0
        self._quiet = 0
        self._peak = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def threshold(self) -> float:
        return max(self.floor_min, self.noise * self.ratio)

    def feed(self, rms: float, chunk: Any = None) -> Optional[List[Any]]:
        """Feed one block. Returns the utterance's chunks when it has ended."""
        self._warm += 1
        if not self._speaking:
            if rms > self.threshold and self._warm > 8:
                self._candidate.append(chunk)
                if len(self._candidate) >= self.start_blocks:
                    self._speaking = True
                    self._frames = list(self._pre) + self._candidate
                    self._candidate = []
                    self._voiced = self.start_blocks
                    self._quiet = 0
                    self._peak = rms
                return None
            # Not speech (or a too-short burst): learn the noise floor.
            if self._candidate:
                for c in self._candidate:
                    self._pre.append(c)
                self._candidate = []
            self._pre.append(chunk)
            # Fast to adapt downwards, slow upwards.
            k = 0.2 if rms < self.noise else 0.02
            self.noise = max(0.0015, (1 - k) * self.noise + k * rms)
            return None

        self._frames.append(chunk)
        self._peak = max(self._peak * 0.998, rms)
        silence_level = max(self.threshold * 0.8, self._peak * 0.2)
        if rms < silence_level:
            self._quiet += 1
        else:
            self._quiet = 0
            self._voiced += 1
        if self._quiet >= self.end_blocks or len(self._frames) >= self.max_blocks:
            frames, voiced = self._frames, self._voiced
            self._speaking = False
            self._frames = []
            self._pre.clear()
            self._quiet = 0
            if voiced >= self.min_blocks:
                return frames
        return None


class ContinuousListener:
    """Keeps the microphone open and emits each utterance as float32 audio.

    Same interface as :class:`~openjarvis.hud.triggers.MicListener`
    (``start / pause / resume / stop / detectors``) so the assistant can use
    either.
    """

    def __init__(
        self,
        *,
        on_utterance: Callable[[Any], None],
        on_level: Optional[Callable[[float], None]] = None,
        on_speech_start: Optional[Callable[[], None]] = None,
        sensitivity: float = 0.5,
        end_silence: float = 0.75,
    ) -> None:
        self._on_utterance = on_utterance
        self._on_level = on_level
        self._on_speech_start = on_speech_start
        self.segmenter = SpeechSegmenter(
            sensitivity=sensitivity, end_silence=end_silence
        )
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def detectors(self) -> List[str]:
        return ["continua"]

    def start(self) -> None:
        self._active.set()
        self._thread = threading.Thread(
            target=self._run, name="jarvis-continuous", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        self._active.clear()

    def resume(self) -> None:
        self.segmenter.reset()
        self._active.set()

    def stop(self) -> None:
        self._stop.set()
        self._active.set()

    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * BLOCK_SECONDS)
        while not self._stop.is_set():
            self._active.wait()
            if self._stop.is_set():
                break
            self.segmenter.reset()
            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block
                ) as stream:
                    level_tick = 0
                    was_speaking = False
                    while self._active.is_set() and not self._stop.is_set():
                        data, _ = stream.read(block)
                        chunk = np.asarray(data, dtype=np.float32).reshape(-1).copy()
                        rms = float(np.sqrt(np.mean(chunk * chunk)) + 1e-7)
                        level_tick += 1
                        if self._on_level is not None and level_tick % 3 == 0:
                            self._on_level(min(1.0, rms * 12))
                        frames = self.segmenter.feed(rms, chunk)
                        if self.segmenter.speaking and not was_speaking:
                            if self._on_speech_start is not None:
                                self._on_speech_start()
                        was_speaking = self.segmenter.speaking
                        if frames:
                            audio = np.concatenate(frames)
                            peak = float(np.max(np.abs(audio))) or 1.0
                            audio = (audio / peak * 0.9).astype(np.float32)
                            self._active.clear()  # the assistant resumes us
                            self._on_utterance(audio)
            except Exception as exc:  # noqa: BLE001 - keep the assistant alive
                logger.warning("Micrófono: %s — reintento en 3 s", exc)
                self._stop.wait(3.0)


class BrowserListener:
    """Speech recognition runs in the HUD page (Edge's Web Speech API).

    The page streams what you say and posts each finished sentence to
    ``/api/heard``. This object only tells the page when to listen: it is
    paused while Jarvis thinks or speaks so it never hears itself.
    """

    def __init__(self, publish: Callable[..., None]) -> None:
        self._publish = publish
        self.active = False

    @property
    def detectors(self) -> List[str]:
        return ["continua", "navegador"]

    def start(self) -> None:
        self.resume()

    def pause(self) -> None:
        self.active = False
        self._publish("listen", on=False)

    def resume(self) -> None:
        self.active = True
        self._publish("listen", on=True)

    def stop(self) -> None:
        self.pause()


__all__ = ["BrowserListener", "ContinuousListener", "SpeechSegmenter"]
