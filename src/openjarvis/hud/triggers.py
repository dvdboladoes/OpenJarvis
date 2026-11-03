"""Wake triggers: double clap and the "Hey Jarvis" wake word.

The microphone is read in 80 ms blocks of 16 kHz mono int16 audio (the frame
size openWakeWord expects). Every block is fed to both detectors; whichever
fires first wakes the assistant.

:class:`ClapDetector` is pure Python + NumPy so it can be unit-tested with
synthetic signals; :class:`MicListener` owns the actual audio stream.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Sequence

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 1280  # 80 ms — openWakeWord's native frame
_WINDOW = 160  # 10 ms analysis windows inside each block
_FULL_SCALE = 32768.0


# ─────────────────────────────────────────────────────────────────────────────
# Double-clap detection
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    t: float
    peak: float
    tail: List[float]


class ClapDetector:
    """Detect two sharp claps in quick succession.

    A clap is a *sharp*, very short transient: the 10 ms peak jumps far
    above the previous moment and the background, and the energy collapses
    again within ~60 ms. Speech and music fail because they rise gradually
    and stay loud. Two such transients between ``min_gap`` and ``max_gap``
    seconds apart trigger the detector.

    Laptop microphones with noise suppression record claps quietly, so the
    test is mostly about sharpness. ``threshold`` (from "Calibrar palmadas")
    sets the minimum peak for this microphone; otherwise ``sensitivity`` (0–1)
    picks one.
    """

    def __init__(
        self,
        sensitivity: float = 0.5,
        *,
        threshold: float = 0.0,
        sample_rate: int = SAMPLE_RATE,
        min_gap: float = 0.1,
        max_gap: float = 1.0,
        cooldown: float = 2.0,
    ) -> None:
        s = max(0.0, min(1.0, float(sensitivity)))
        self.sample_rate = sample_rate
        # Minimum 10 ms peak (fraction of full scale). Low on purpose: a clap
        # from across the room reaches a laptop mic very quietly.
        self.peak_threshold = threshold if threshold > 0 else 0.012 - 0.01 * s
        self.contrast = 14.0 - 8.0 * s  # peak vs. background RMS
        self.attack = 3.0  # peak vs. the window just before: claps are sudden
        # Tail RMS (30-80 ms after the peak) must fall below peak * ratio.
        # Generous, because far away the room's echo lingers.
        self.decay_ratio = 0.5
        self.tail_windows = 8
        self.refractory = 0.08
        # Rhythm: two *isolated* knocks. Quiet before the first and after the
        # second, so typing (many knocks in a row) never counts as claps.
        self.quiet_before = 0.35
        self.quiet_after = 0.3
        self.min_bright = 0.6  # see process()
        self.max_mismatch = 5.0  # the two claps must be of similar strength
        self._bright = 1.0
        self._peaks: Deque[float] = deque(maxlen=6)
        self._prev_peak = 0.0
        self.min_gap = min_gap
        self.max_gap = max_gap
        self.cooldown = cooldown

        self._t = 0.0  # stream clock, seconds
        self._background = 0.001
        self._candidate: Optional[_Candidate] = None
        self._claps: Deque[float] = deque(maxlen=6)
        self._pending: Optional[float] = None  # second clap awaiting quiet
        self._last_onset = -10.0
        self._last_trigger = -10.0

    def reset(self) -> None:
        self._candidate = None
        self._pending = None
        self._claps.clear()

    def process(self, block: Sequence[int] | "object") -> bool:
        """Feed one block of int16 samples; return True on a double clap."""
        import numpy as np

        samples = np.asarray(block, dtype=np.float32) / _FULL_SCALE
        triggered = False
        dt = _WINDOW / self.sample_rate
        for start in range(0, len(samples) - _WINDOW + 1, _WINDOW):
            win = samples[start : start + _WINDOW]
            peak = float(np.max(np.abs(win)))
            rms = float(np.sqrt(np.mean(win * win)))
            # Brightness: a clap is a crack full of treble; thuds, footsteps,
            # voices and music beats are much darker. ~1.4 for white noise,
            # ~0.4 at 1 kHz, ~0.07 for a deep hum.
            diff = np.diff(win)
            self._bright = float(np.sqrt(np.mean(diff * diff))) / (rms + 1e-9)
            if self._step(peak, rms):
                triggered = True
            self._t += dt
        return triggered

    def _step(self, peak: float, rms: float) -> bool:
        triggered = False

        # 0. A double clap is confirmed once the room stays quiet after it.
        if self._pending is not None and self._t - self._pending >= self.quiet_after:
            self._pending = None
            if self._t - self._last_trigger >= self.cooldown:
                self._last_trigger = self._t
                self._claps.clear()
                triggered = True

        # 1. Confirm or reject a pending candidate once its tail is collected.
        cand = self._candidate
        if cand is not None:
            cand.tail.append(rms)
            if len(cand.tail) >= self.tail_windows:
                tail = cand.tail[3:]
                tail_rms = sum(tail) / max(1, len(tail))
                self._candidate = None
                if tail_rms < cand.peak * self.decay_ratio:
                    self._register_clap(cand.t, cand.peak)
            return triggered

        # 2. Look for a new onset.
        prev, self._prev_peak = self._prev_peak, peak
        is_onset = (
            peak >= self.peak_threshold
            and peak >= self._background * self.contrast
            and peak >= max(prev, 1e-4) * self.attack
            and self._bright >= self.min_bright
            and (self._t - self._last_onset) >= self.refractory
        )
        if is_onset:
            self._last_onset = self._t
            self._candidate = _Candidate(t=self._t, peak=peak, tail=[])
            return triggered

        # 3. Track the background level slowly (only outside transients).
        # The floor is tiny: noise-suppressing laptop mics are almost silent.
        self._background = 0.98 * self._background + 0.02 * max(rms, 0.00005)
        return triggered

    def _register_clap(self, t: float, peak: float = 1.0) -> None:
        """A sharp knock at *t*: pair it with the previous one if the rhythm fits."""
        if self._pending is not None:
            self._pending = None  # a third knock: that was not a double clap
            self._claps.append(t)
            self._peaks.append(peak)
            return
        prev = self._claps[-1] if self._claps else None
        prev_peak = self._peaks[-1] if self._peaks else peak
        before = self._claps[-2] if len(self._claps) >= 2 else None
        self._claps.append(t)
        self._peaks.append(peak)
        if prev is None or not self.min_gap <= t - prev <= self.max_gap:
            return
        if before is not None and prev - before < self.quiet_before:
            return  # part of a longer series (typing, knocking…)
        if max(peak, prev_peak) > self.max_mismatch * min(peak, prev_peak):
            return  # two unrelated noises (a loud bang, then a faint click)
        self._pending = t


def calibrate_claps(samples: "object", sample_rate: int = SAMPLE_RATE) -> dict:
    """Find the claps in a few seconds of audio and suggest a threshold.

    Returns ``{"palmadas": n, "umbral": float}``; ``umbral`` is 0 when no clap
    was heard. Used by Ajustes → "Calibrar palmadas".
    """
    import numpy as np

    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.dtype != np.float32 or np.max(np.abs(x)) > 1.5:
        x = x / _FULL_SCALE
    n = len(x) // _WINDOW
    if n < 10:
        return {"palmadas": 0, "umbral": 0.0}
    w = x[: n * _WINDOW].reshape(n, _WINDOW)
    peaks = np.abs(w).max(axis=1)
    rms = np.sqrt((w * w).mean(axis=1))
    noise = float(np.median(rms)) + 1e-5
    found = []
    i = 1
    while i < n - 6:
        sharp = peaks[i] >= 4 * max(peaks[i - 1], 1e-4)
        loud = peaks[i] >= 20 * noise
        decays = float(rms[i + 2 : i + 6].mean()) < peaks[i] * 0.4
        if sharp and loud and decays:
            found.append(float(peaks[i]))
            i += 8  # skip this clap's tail
        else:
            i += 1
    if not found:
        return {"palmadas": 0, "umbral": 0.0}
    # A third of the claps it heard: claps from further away (quieter) still
    # count, but never below what the room's noise allows.
    claps = sorted(found)[-3:]
    umbral = max(0.3 * float(np.median(claps)), 10 * noise)
    return {"palmadas": len(found), "umbral": round(umbral, 5)}


# ─────────────────────────────────────────────────────────────────────────────
# "Hey Jarvis" wake word (openWakeWord)
# ─────────────────────────────────────────────────────────────────────────────


class WakeWordDetector:
    """Thin wrapper around openWakeWord's pre-trained ``hey_jarvis`` model."""

    MODEL_NAME = "hey_jarvis"

    def __init__(self, sensitivity: float = 0.5, cooldown: float = 2.5) -> None:
        s = max(0.0, min(1.0, float(sensitivity)))
        self.threshold = 0.8 - 0.5 * s
        self.cooldown = cooldown
        self._last = 0.0
        self._model = None

    def load(self) -> None:
        """Load the model, downloading it on first use. Raises if unavailable."""
        import openwakeword
        from openwakeword.model import Model

        try:
            from openwakeword.utils import download_models

            download_models([self.MODEL_NAME])
        except Exception:  # noqa: BLE001 - already cached or offline
            logger.debug("openWakeWord model download skipped", exc_info=True)

        kwargs = {"wakeword_models": [self.MODEL_NAME]}
        try:
            self._model = Model(inference_framework="onnx", **kwargs)
        except TypeError:  # older openwakeword without inference_framework
            self._model = Model(**kwargs)
        logger.info("openWakeWord %s ready", getattr(openwakeword, "__version__", ""))

    @property
    def ready(self) -> bool:
        return self._model is not None

    def process(self, block: "object") -> bool:
        if self._model is None:
            return False
        scores = self._model.predict(block)
        score = max(scores.values()) if scores else 0.0
        now = time.monotonic()
        if score >= self.threshold and now - self._last > self.cooldown:
            self._last = now
            self._model.reset()
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Microphone loop
# ─────────────────────────────────────────────────────────────────────────────


class MicListener:
    """Background microphone reader that runs the wake detectors.

    ``on_wake(source)`` is called with ``"palmadas"`` or ``"voz"``.
    ``on_level(level)`` receives a 0–1 input level ~12 times per second so the
    HUD can visualise that Jarvis is listening.

    The stream is paused while the assistant records or speaks, so it never
    hears itself and Windows never has to share the device.
    """

    def __init__(
        self,
        *,
        on_wake: Callable[[str], None],
        on_level: Optional[Callable[[float], None]] = None,
        clap: Optional[ClapDetector] = None,
        wake: Optional[WakeWordDetector] = None,
    ) -> None:
        self._on_wake = on_wake
        self._on_level = on_level
        self._clap = clap
        self._wake = wake
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def detectors(self) -> List[str]:
        names = []
        if self._clap is not None:
            names.append("palmadas")
        if self._wake is not None and self._wake.ready:
            names.append("voz")
        return names

    def start(self) -> None:
        self._active.set()
        self._thread = threading.Thread(
            target=self._run, name="jarvis-mic", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        self._active.clear()

    def resume(self) -> None:
        if self._clap is not None:
            self._clap.reset()
        self._active.set()

    def stop(self) -> None:
        self._stop.set()
        self._active.set()

    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd

        while not self._stop.is_set():
            self._active.wait()
            if self._stop.is_set():
                break
            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=BLOCK_SAMPLES,
                ) as stream:
                    while self._active.is_set() and not self._stop.is_set():
                        data, _ = stream.read(BLOCK_SAMPLES)
                        block = np.asarray(data, dtype=np.int16).reshape(-1)
                        if self._on_level is not None:
                            peak = float(np.max(np.abs(block))) / _FULL_SCALE
                            self._on_level(min(1.0, peak * 1.6))
                        source = self._detect(block)
                        if source:
                            self._active.clear()
                            self._on_wake(source)
            except Exception as exc:  # noqa: BLE001 - keep the assistant alive
                logger.warning("Microphone error: %s — retrying in 3 s", exc)
                self._stop.wait(3.0)

    def _detect(self, block: "object") -> Optional[str]:
        if self._clap is not None and self._clap.process(block):
            return "palmadas"
        if self._wake is not None and self._wake.process(block):
            return "voz"
        return None


__all__ = [
    "BLOCK_SAMPLES",
    "SAMPLE_RATE",
    "ClapDetector",
    "MicListener",
    "calibrate_claps",
    "WakeWordDetector",
]
