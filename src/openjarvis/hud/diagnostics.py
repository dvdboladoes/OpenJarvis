"""``jarvis hud --diagnostico``: check every part of the voice assistant."""

from __future__ import annotations

import time
import traceback
from typing import Any

from openjarvis.hud.config import HudConfig


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def run_diagnostics(cfg: HudConfig, console: Any, *, mic: bool = True) -> None:
    ok = "[green]OK[/green]"
    bad = "[red]FALLO[/red]"
    console.print(f"\n[bold cyan]Diagnóstico de Jarvis[/bold cyan]  ·  {cfg.path}\n")

    # 1. Brain -------------------------------------------------------------
    console.print("[bold]1. Cerebro (modelo de IA)[/bold]")
    from openjarvis.hud.brain import JarvisBrain, is_cloud_model

    brain = JarvisBrain(cfg)
    try:
        sdk = brain._ensure_sdk()  # noqa: SLF001
        console.print(
            f"   modelo en jarvis.toml: {cfg.modelo or '(vacío = por defecto)'}"
        )
        console.print(
            "   modelo por defecto de OpenJarvis: "
            f"{sdk.config.intelligence.default_model or '(ninguno)'}"
        )
        sdk._ensure_engine()  # noqa: SLF001
        console.print(f"   motor: {sdk._resolved_engine_key}")  # noqa: SLF001
        if not is_cloud_model(cfg.modelo):
            try:
                models = sdk._engine.list_models()  # noqa: SLF001
                console.print(
                    f"   modelos instalados: {', '.join(models) or '(ninguno)'}"
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"   no se pudo listar modelos: {_short(exc)}")
        model = brain.resolve_model()
        console.print(f"   usando: [cyan]{model}[/cyan]")
        t0 = time.monotonic()
        reply = brain.reply("Responde solo: sistemas en línea.")
        console.print(f"   respuesta ({time.monotonic() - t0:.1f} s): «{reply}»  {ok}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"   {bad} {_short(exc)}")
        console.print(f"[dim]{traceback.format_exc(limit=3)}[/dim]")
    finally:
        brain.close()

    # 2. Voice -------------------------------------------------------------
    console.print("\n[bold]2. Voz[/bold]")
    from openjarvis.hud.voice import Ear, Speaker

    speaker = Speaker(cfg)
    try:
        audio = speaker.synthesize("Diagnóstico en curso, señor.")
        if audio:
            console.print(
                f"   motor: {speaker.backend_name}  ({len(audio)} bytes)  {ok}"
            )
            speaker.play(audio)
        else:
            console.print(
                f"   {bad} ningún motor de voz disponible (uv sync --extra hud)"
            )
    except Exception as exc:  # noqa: BLE001
        console.print(f"   {bad} {_short(exc)}")

    if not mic:
        return

    # 3. Microphone --------------------------------------------------------
    console.print("\n[bold]3. Micrófono[/bold]")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        console.print(f"   entrada por defecto: {dev.get('name')}  {ok}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"   {bad} {_short(exc)}")
        return

    # 4. Hearing -----------------------------------------------------------
    console.print("\n[bold]4. Oído[/bold]")
    ear = Ear(cfg)
    try:
        t0 = time.monotonic()
        if not ear.ready:
            console.print(
                f"   {bad} reconocimiento de voz no instalado (uv sync --extra desktop)"
            )
            return
        console.print(f"   {ear.description} cargado en {time.monotonic() - t0:.1f} s")
        console.print(
            "   [yellow]Habla ahora: di una frase normal, por ejemplo "
            "«¿qué tengo hoy en la agenda?»[/yellow]"
        )
        peak = {"v": 0.0}

        def _lvl(v: float) -> None:
            peak["v"] = max(peak["v"], v)

        ear.on_level = _lvl
        t0 = time.monotonic()
        text = ear.listen(startup_silence=8.0)
        console.print(f"   nivel máximo del micro: {peak['v']:.2f} (ideal 0.3–1.0)")
        if text:
            console.print(
                f"   he entendido ({time.monotonic() - t0:.1f} s): «{text}»  {ok}"
            )
        else:
            console.print(f"   {bad} no he oído nada. Revisa el micrófono de Windows.")
    except Exception as exc:  # noqa: BLE001
        console.print(f"   {bad} {_short(exc)}")

    console.print(
        "\n[dim]Copia todo este texto y pásaselo a Claude si algo sale en rojo.[/dim]\n"
    )


__all__ = ["run_diagnostics"]
