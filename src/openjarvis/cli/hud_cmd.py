"""``jarvis hud`` — the always-on voice Jarvis with its holographic interface."""

from __future__ import annotations

import sys

import click


def _redirect_headless_output() -> None:
    """Under pythonw (auto-start) there is no console; log to a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from openjarvis.core.paths import get_config_dir

    log_path = get_config_dir() / "jarvis-hud.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


@click.command(
    "hud",
    help=(
        "Arranca Jarvis por voz con su interfaz holográfica. Se despierta con "
        "dos palmadas o diciendo «Hey Jarvis», te da el briefing y conversa."
    ),
)
@click.option(
    "--demo", is_flag=True, help="Demostración con datos de ejemplo (sin micro ni IA)."
)
@click.option(
    "--sin-micro", "no_mic", is_flag=True, help="No escuchar el micrófono (solo botón)."
)
@click.option(
    "--sin-ventana", "no_window", is_flag=True, help="No abrir la ventana del HUD."
)
@click.option(
    "--pantalla-completa",
    "fullscreen",
    is_flag=True,
    help="Abrir el HUD a pantalla completa.",
)
@click.option("--puerto", "port", type=int, default=None, help="Puerto local del HUD.")
@click.option(
    "--configurar",
    "edit_config",
    is_flag=True,
    help="Abrir el archivo de configuración.",
)
@click.option(
    "--autoarranque",
    "autostart",
    is_flag=True,
    help="Arrancar Jarvis al iniciar Windows.",
)
@click.option(
    "--quitar-autoarranque",
    "no_autostart",
    is_flag=True,
    help="Quitar el arranque automático.",
)
@click.option(
    "--diagnostico",
    "diagnose",
    is_flag=True,
    help="Probar cerebro, voz, micrófono y oído, y decir qué falla.",
)
def hud(
    diagnose: bool,
    demo: bool,
    no_mic: bool,
    no_window: bool,
    fullscreen: bool,
    port: int | None,
    edit_config: bool,
    autostart: bool,
    no_autostart: bool,
) -> None:
    _redirect_headless_output()

    from rich.console import Console

    from openjarvis.hud.config import ensure_config_file, load_hud_config

    console = Console()

    if edit_config:
        path = ensure_config_file()
        console.print(f"Configuración: [cyan]{path}[/cyan]")
        click.launch(str(path))
        return

    if diagnose:
        from openjarvis.hud.diagnostics import run_diagnostics

        run_diagnostics(load_hud_config(), console, mic=not no_mic)
        return

    if autostart or no_autostart:
        from openjarvis.hud.launcher import install_autostart, remove_autostart

        if no_autostart:
            removed = remove_autostart()
            console.print(
                "Arranque automático quitado." if removed else "No estaba activado."
            )
            return
        try:
            target = install_autostart()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1)
        console.print(
            f"[green]Listo.[/green] Jarvis arrancará solo al iniciar Windows.\n"
            f"[dim]{target}[/dim]"
        )
        return

    cfg = load_hud_config()
    if port:
        cfg.puerto = port

    if demo:
        from openjarvis.hud.demo import DemoRunner

        runner = DemoRunner(cfg)
        assistant = None
        runner.start()
        url = runner.server.url
    else:
        from openjarvis.hud.assistant import JarvisAssistant

        assistant = JarvisAssistant(cfg, mic=not no_mic)
        runner = None
        try:
            assistant.start()
        except OSError as exc:
            console.print(
                f"[red]No se pudo abrir el puerto {cfg.puerto}: {exc}[/red]\n"
                "¿Ya hay otro Jarvis abierto? Ciérralo o usa --puerto 8766."
            )
            raise SystemExit(1)
        url = assistant.server.url

    console.print(
        "\n[bold cyan]J.A.R.V.I.S.[/bold cyan] en línea  ·  "
        f"interfaz: [cyan]{url}[/cyan]\n"
        f"[dim]Configuración: {cfg.path}[/dim]\n"
        "[dim]Despiértalo con dos palmadas o diciendo «Hey Jarvis». "
        "Ctrl+C para salir.[/dim]\n"
    )

    if cfg.abrir_ventana and not no_window:
        from openjarvis.hud.launcher import open_window

        open_window(url, fullscreen=fullscreen or cfg.pantalla_completa)

    try:
        if assistant is not None:
            assistant.wait()
        else:
            import time

            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Apagando sistemas…[/dim]")
    finally:
        if assistant is not None:
            assistant.stop()
        if runner is not None:
            runner.stop()


__all__ = ["hud"]
