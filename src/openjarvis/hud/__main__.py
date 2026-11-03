"""``python -m openjarvis.hud`` — start Jarvis without loading the whole CLI.

The full ``jarvis`` command imports every OpenJarvis feature (~15 s on a
laptop); this entry point only loads Jarvis itself (~1 s).
"""

from openjarvis.hud.cli import hud

if __name__ == "__main__":
    hud(prog_name="jarvis hud")
