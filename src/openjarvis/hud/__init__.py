"""Jarvis HUD — always-on voice assistant with a holographic web interface.

Wake it with a double clap or by saying "Hey Jarvis"; it greets you with a
spoken briefing (calendar, inbox, business metrics) and then stays in a
hands-free conversation. Everything is mirrored live on a HUD page served
locally and opened as a frameless window.

Entry point: ``jarvis hud`` (see :mod:`openjarvis.cli.hud_cmd`).
"""

from openjarvis.hud.config import HudConfig, load_hud_config

__all__ = ["HudConfig", "load_hud_config"]
