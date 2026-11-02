"""Edge TTS backend — free Microsoft neural voices (no API key).

Uses the ``edge-tts`` package, which talks to the same neural voice service
as Microsoft Edge's "Read aloud". Voices are high quality in many languages,
including Spanish (``es-ES-AlvaroNeural``, ``es-ES-ElviraNeural``…), which
makes it the default voice for the Spanish Jarvis HUD.

Requires network access but no account or key.
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from typing import List

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

DEFAULT_VOICE = "es-ES-AlvaroNeural"

# (voice id, label shown in the Jarvis HUD voice picker)
SPANISH_VOICES = [
    ("es-ES-AlvaroNeural", "Álvaro · España"),
    ("es-ES-ElviraNeural", "Elvira · España"),
    ("es-ES-XimenaNeural", "Ximena · España"),
    ("en-US-AndrewMultilingualNeural", "Andrew · multilingüe, muy natural"),
    ("en-US-BrianMultilingualNeural", "Brian · multilingüe, cálido"),
    ("en-US-AvaMultilingualNeural", "Ava · multilingüe, expresiva"),
    ("es-MX-JorgeNeural", "Jorge · México"),
    ("es-AR-TomasNeural", "Tomás · Argentina"),
]
_COMMON_VOICES = [v for v, _ in SPANISH_VOICES] + [
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
]


def _rate_from_speed(speed: float) -> str:
    """Convert a speed multiplier (1.0 = normal) to edge-tts' ``+N%`` syntax."""
    pct = int(round((float(speed or 1.0) - 1.0) * 100))
    pct = max(-50, min(100, pct))
    return f"{pct:+d}%"


async def _synthesize_async(
    text: str, voice: str, rate: str, pitch: str, volume: str = "+0%"
) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(
        text, voice, rate=rate, pitch=pitch, volume=volume
    )
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


def _run_coro(coro):  # type: ignore[no-untyped-def]
    """Run *coro* even if the calling thread already has a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


@TTSRegistry.register("edge")
class EdgeTTSBackend(TTSBackend):
    """Microsoft Edge neural voices via ``edge-tts`` (MP3 output)."""

    backend_id = "edge"

    def __init__(self, pitch: str = "+0Hz") -> None:
        self._pitch = pitch

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = DEFAULT_VOICE,
        speed: float = 1.0,
        output_format: str = "mp3",
        pitch: str = "",
        volume: str = "+0%",
    ) -> TTSResult:
        """Synthesize *text*. ``pitch`` (e.g. ``"+6Hz"``) and ``volume`` (e.g.
        ``"+10%"``) let callers colour the delivery with emotion."""
        voice = voice_id or DEFAULT_VOICE
        audio = _run_coro(
            _synthesize_async(
                text, voice, _rate_from_speed(speed), pitch or self._pitch, volume
            )
        )
        # edge-tts streams MP3; soundfile (libsndfile >= 1.1) decodes it, so the
        # shared play_wav() helper can play it even though it is not WAV.
        return TTSResult(
            audio=audio or b"",
            format="mp3",
            voice_id=voice,
            sample_rate=24000,
            metadata={"backend": "edge"},
        )

    def available_voices(self) -> List[str]:
        return list(_COMMON_VOICES)

    def health(self) -> bool:
        return importlib.util.find_spec("edge_tts") is not None


__all__ = ["DEFAULT_VOICE", "SPANISH_VOICES", "EdgeTTSBackend"]
