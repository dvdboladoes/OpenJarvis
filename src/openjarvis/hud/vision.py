"""Let Jarvis see: the camera (live in the HUD) and the screen.

"¿Qué hago aquí?", "¿qué ves?", "mira esto" → a frame from the camera that
the HUD shows live; "¿qué hay en mi pantalla?", "ayúdame con esto de la
pantalla" → a screenshot. The image goes to a vision-capable model: Claude
when it is the brain, otherwise a local Ollama vision model.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import unicodedata
import uuid
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Local Ollama models that can see, best first.
VISION_MODELS = (
    "qwen2.5vl",
    "gemma3",
    "llama3.2-vision",
    "minicpm-v",
    "llava",
    "granite3.2-vision",
    "moondream",
)
SUGGESTED_LOCAL = "qwen2.5vl:3b"


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[¿?¡!.,;:]", " ", t)).strip()


_SCREEN = re.compile(
    r"\b(pantalla|monitor|en el ordenador|lo que estoy viendo|captura)\b"
)
_CAMERA = re.compile(
    r"\b(que ves|que estas viendo|mira esto|mira aqui|mirame|que hago aqui|"
    r"que es esto|que tengo (?:aqui|en la mano)|camara|que llevo puesto|"
    r"como me queda|que te parece esto|que hago con esto|ayudame con esto)\b"
)


def parse_vision(text: str) -> Optional[Tuple[str, str]]:
    """Return ``("pantalla" | "camara", question)`` if the user wants Jarvis to look."""
    t = _fold(text)
    if _SCREEN.search(t) and re.search(r"\b(que|mira|ayuda|explica|lee|ves|hago)\b", t):
        return "pantalla", text.strip()
    if _CAMERA.search(t):
        return "camara", text.strip()
    return None


_WATCH_ON = re.compile(
    r"\b(mira|vigila|observa|sigue|ve|controla) (mi |la )?pantalla\b"
    r"|\b(modo pantalla|ver mi pantalla|ve lo que hago|mira lo que hago)\b"
)
_WATCH_OFF = re.compile(
    r"\b(deja de (mirar|ver|vigilar)|no mires|para de mirar|quita el modo pantalla"
    r"|desactiva el modo pantalla)\b"
)


def parse_screen_watch(text: str) -> Optional[bool]:
    """True = start watching the screen, False = stop, None = not about that."""
    t = _fold(text)
    if _WATCH_OFF.search(t):
        return False
    if _WATCH_ON.search(t) and len(t) < 60:
        return True
    return None


def pick_vision_model(
    main_model: str, installed: List[str], is_cloud: bool
) -> Optional[str]:
    """The model to use for images, or None if none can see."""
    if is_cloud:
        return main_model  # Claude / GPT / Gemini all accept images
    for family in VISION_MODELS:
        for name in installed:
            if name.lower().startswith(family):
                return name
    return None


def encode_jpeg(image, max_side: int = 1280, quality: int = 80) -> str:  # type: ignore[no-untyped-def]
    """PIL image → base64 JPEG, downscaled so it is quick to send and process."""
    image = image.convert("RGB")
    w, h = image.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        image = image.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def capture_screen() -> str:
    """Screenshot of the main screen as base64 JPEG."""
    from PIL import ImageGrab

    return encode_jpeg(ImageGrab.grab(all_screens=False))


class FrameBroker:
    """Ask the HUD page for a camera frame and wait for it.

    The camera is opened by the browser window (getUserMedia), which also
    shows it live; the server publishes a ``camera_request`` event and the page
    answers by POSTing a JPEG to ``/api/frame``.
    """

    def __init__(self, publish) -> None:  # type: ignore[no-untyped-def]
        self._publish = publish
        self._waiting: Dict[str, Tuple[threading.Event, List[str]]] = {}
        self._lock = threading.Lock()

    def request(self, timeout: float = 6.0) -> Optional[str]:
        rid = uuid.uuid4().hex[:8]
        done, box = threading.Event(), []
        with self._lock:
            self._waiting[rid] = (done, box)
        self._publish("camera_request", id=rid)
        done.wait(timeout)
        with self._lock:
            self._waiting.pop(rid, None)
        return box[0] if box else None

    def deliver(self, rid: str, image_b64: str) -> bool:
        with self._lock:
            entry = self._waiting.get(rid)
        if entry is None or not image_b64:
            return False
        data = image_b64.split(",", 1)[-1]  # strip "data:image/jpeg;base64,"
        entry[1].append(data)
        entry[0].set()
        return True


__all__ = [
    "FrameBroker",
    "SUGGESTED_LOCAL",
    "capture_screen",
    "encode_jpeg",
    "parse_screen_watch",
    "parse_vision",
    "pick_vision_model",
]
