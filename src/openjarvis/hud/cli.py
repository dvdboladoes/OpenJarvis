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
    # Keep a record of timings and warnings ("Tiempos: total …") to diagnose
    # slow answers; the HTTP libraries only log problems.
    import logging

    logging.basicConfig(
        stream=stream,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
    "--instalar",
    "install",
    is_flag=True,
    help="Crear los accesos directos «Jarvis» (escritorio y menú Inicio).",
)
@click.option(
    "--segundo-plano",
    "background",
    is_flag=True,
    help="Arrancar oculto: se despierta con palmadas o «Jarvis, enciéndete».",
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
    background: bool = False,
    install: bool = False,
) -> None:
    _redirect_headless_output()

    from openjarvis.hud.launcher import load_user_keys

    load_user_keys()  # keys saved with setx, even if this process missed them

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

    if install:
        from openjarvis.hud.launcher import install_app

        for lnk in install_app():
            console.print(f"[green]Acceso directo:[/green] {lnk}")
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

    if not demo and (
        _already_running(cfg.puerto, background) or not _single_instance()
    ):
        console.print("Jarvis ya estaba abierto: lo traigo al frente.")
        return

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

        open_window(
            url,
            fullscreen=fullscreen or cfg.pantalla_completa,
            browser=cfg.navegador,
        )
        if not (fullscreen or cfg.pantalla_completa or background):
            from openjarvis.hud.window import maximize_when_ready

            maximize_when_ready()

    tray = None
    if assistant is not None:
        import threading

        from openjarvis.hud.tray import Tray

        tray = Tray(
            on_show=lambda: threading.Thread(
                target=assistant.exit_background, daemon=True
            ).start(),
            on_background=lambda: threading.Thread(
                target=assistant.enter_background, daemon=True
            ).start(),
            on_quit=lambda: assistant._api_quit({}),  # noqa: SLF001
        )
        tray.start()
        if background:
            threading.Thread(
                target=_go_background, args=(assistant,), daemon=True
            ).start()

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
        if tray is not None:
            tray.stop()
        if assistant is not None:
            assistant.stop()
        if runner is not None:
            runner.stop()
    if assistant is not None:
        # Leave nothing behind: the tray icon and audio libraries keep threads
        # alive that would otherwise leave a "ghost" Jarvis listening.
        import os

        os._exit(0)


_INSTANCE_LOCK = None


def _single_instance() -> bool:
    """True if this is the only Jarvis running (a Windows named mutex)."""
    global _INSTANCE_LOCK
    import sys

    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _INSTANCE_LOCK = kernel32.CreateMutexW(
        None, False, "Local\\JarvisHUDSingleInstance"
    )
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _already_running(port: int, background: bool) -> bool:
    """If Jarvis is already open, show it (unless starting hidden) and say so."""
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/{'config' if background else 'wake'}",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
            return resp.status == 200
    except OSError:
        return False


def _go_background(assistant) -> None:  # type: ignore[no-untyped-def]
    """Start hidden: wait for the window to exist, then hide it silently."""
    import time

    from openjarvis.hud import window

    for _ in range(40):
        if window.find():
            break
        time.sleep(0.25)
    assistant.enter_background(announce=False)


__all__ = ["hud"]
