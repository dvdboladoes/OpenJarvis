"""Jarvis' icon next to the clock (system tray) and the app icon itself."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def make_icon(size: int = 256) -> Any:
    """Arc-reactor style icon drawn with Pillow (no image files needed)."""
    from PIL import Image, ImageDraw, ImageFilter

    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = big / 2
    d.ellipse((big * 0.03,) * 2 + (big * 0.97,) * 2, fill=(4, 12, 22, 255))
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse(
        (c - big * 0.30, c - big * 0.30, c + big * 0.30, c + big * 0.30),
        fill=(60, 220, 255, 170),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(big * 0.08))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    for r, w, col in (
        (0.44, 0.035, (95, 227, 255, 255)),
        (0.33, 0.02, (95, 227, 255, 200)),
    ):
        d.ellipse(
            (c - big * r, c - big * r, c + big * r, c + big * r),
            outline=col,
            width=int(big * w),
        )
    for i in range(10):  # segmented ring
        start = i * 36 + 4
        d.arc(
            (c - big * 0.39, c - big * 0.39, c + big * 0.39, c + big * 0.39),
            start,
            start + 26,
            fill=(160, 240, 255, 255),
            width=int(big * 0.05),
        )
    d.ellipse(
        (c - big * 0.15, c - big * 0.15, c + big * 0.15, c + big * 0.15),
        fill=(225, 252, 255, 255),
    )
    return img.resize((size, size), Image.Resampling.LANCZOS)


def save_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    make_icon(256).save(
        path,
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    return path


class Tray:
    """System-tray icon: show Jarvis, send it to the background, or quit."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_background: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_show = on_show
        self._on_background = on_background
        self._on_quit = on_quit
        self._icon: Any = None

    def start(self) -> bool:
        try:
            import pystray
        except ImportError:
            logger.info("pystray no instalado: sin icono en la bandeja")
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar Jarvis", lambda: self._on_show(), default=True),
            pystray.MenuItem("Segundo plano", lambda: self._on_background()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda: self._on_quit()),
        )
        self._icon = pystray.Icon("jarvis", make_icon(64), "J.A.R.V.I.S.", menu)
        self._icon.run_detached()
        return True

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["Tray", "make_icon", "save_ico"]
