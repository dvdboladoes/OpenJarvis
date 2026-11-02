"""Tests for the Jarvis HUD (voice assistant + web interface)."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

np = pytest.importorskip("numpy")

from openjarvis.hud.brain import clean_for_speech, is_sleep_command  # noqa: E402
from openjarvis.hud.config import DEFAULT_TEMPLATE, parse_hud_config  # noqa: E402
from openjarvis.hud.context import (  # noqa: E402
    Snapshot,
    parse_digest_sections,
    parse_metrics,
)
from openjarvis.hud.server import EventHub, HudServer  # noqa: E402
from openjarvis.hud.triggers import BLOCK_SAMPLES, ClapDetector  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

SR = 16000


# ── clap detection ──────────────────────────────────────────────────────────


def _noise(seconds: float, amp: float = 300.0, seed: int = 0) -> "np.ndarray":
    return np.random.default_rng(seed).normal(0, amp, int(SR * seconds))


def _clap(seed: int = 1) -> "np.ndarray":
    n = int(0.03 * SR)
    env = np.exp(-np.arange(n) / (0.004 * SR))
    return np.random.default_rng(seed).normal(0, 1, n) * env * 25000


def _tone(seconds: float) -> "np.ndarray":
    t = np.arange(int(SR * seconds)) / SR
    return np.sin(2 * np.pi * 180 * t) * 12000 * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))


def _triggers(signal: "np.ndarray", sensitivity: float = 0.5) -> int:
    det = ClapDetector(sensitivity)
    pcm = np.clip(signal, -32767, 32767).astype(np.int16)
    hits = 0
    for i in range(0, len(pcm) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        hits += det.process(pcm[i : i + BLOCK_SAMPLES])
    return hits


def test_double_clap_triggers_once() -> None:
    sig = np.concatenate([_noise(1), _clap(), _noise(0.35), _clap(2), _noise(1)])
    assert _triggers(sig) == 1


@pytest.mark.parametrize(
    "signal",
    [
        np.concatenate([_noise(1), _clap(), _noise(2)]),  # single clap
        np.concatenate(
            [_noise(1), _clap(), _noise(1.6), _clap(2), _noise(1)]
        ),  # too far apart
        np.concatenate([_noise(1), _tone(2), _noise(1)]),  # loud sustained sound
        _noise(3),  # silence
    ],
)
def test_no_false_trigger(signal: "np.ndarray") -> None:
    assert _triggers(signal) == 0


# ── config ──────────────────────────────────────────────────────────────────


def test_default_template_parses() -> None:
    cfg = parse_hud_config(tomllib.loads(DEFAULT_TEMPLATE))
    assert cfg.honorific == "señor"
    assert cfg.motor_voz == "edge"
    assert cfg.palmadas and cfg.palabra_clave
    assert cfg.puerto == 8765


def test_config_clamps_and_tolerates_missing_sections() -> None:
    cfg = parse_hud_config(
        {"activacion": {"sensibilidad_palmadas": 7}, "interfaz": {"puerto": 1}}
    )
    assert cfg.sensibilidad_palmadas == 0.95
    assert cfg.puerto == 1024
    assert cfg.fuentes  # defaults kept


# ── context parsing ─────────────────────────────────────────────────────────


def test_parse_metrics_csv_with_header() -> None:
    m = parse_metrics("nombre,valor,cambio\nLeads,23,+18%\nVentas,1.240 €,-2%\n")
    assert [(x.nombre, x.valor, x.tendencia) for x in m] == [
        ("Leads", "23", "up"),
        ("Ventas", "1.240 €", "down"),
    ]


def test_parse_metrics_json() -> None:
    assert parse_metrics('{"Leads": 5}')[0].valor == "5"
    assert parse_metrics('[{"name": "MRR", "value": "9k"}]')[0].nombre == "MRR"


def test_parse_digest_sections_strips_sources_and_errors() -> None:
    text = "=== CALENDAR ===\n[gcalendar] 10:00 Reunión\n\n=== ERRORS ===\nboom\n"
    assert parse_digest_sections(text) == {"CALENDAR": ["10:00 Reunión"]}


def test_snapshot_prompt_mentions_everything() -> None:
    snap = Snapshot(secciones={"CALENDAR": ["10:00 Reunión"]}, tiempo="Soleado")
    snap.metricas = parse_metrics("Leads,23")
    prompt = snap.as_prompt()
    assert "10:00 Reunión" in prompt and "Soleado" in prompt and "Leads: 23" in prompt
    assert snap.to_dict()["agenda"] == ["10:00 Reunión"]


# ── speech helpers ──────────────────────────────────────────────────────────


def test_clean_for_speech_removes_markdown() -> None:
    out = clean_for_speech(
        "**Hola** señor\n- uno\n- dos [web](http://x) <think>x</think>"
    )
    assert out == "Hola señor uno dos web"


def test_sleep_command() -> None:
    assert is_sleep_command("Vale, gracias Jarvis.")
    assert not is_sleep_command("¿Qué tengo mañana?")


def test_edge_rate_conversion() -> None:
    from openjarvis.speech.edge_tts_backend import _rate_from_speed

    assert _rate_from_speed(1.0) == "+0%"
    assert _rate_from_speed(1.25) == "+25%"
    assert _rate_from_speed(0.1) == "-50%"


# ── HUD web server ──────────────────────────────────────────────────────────


def test_server_serves_page_events_and_actions() -> None:
    hub = EventHub()
    calls = []
    server = HudServer(
        hub, {"activate": lambda p: calls.append(p) or {"started": True}}, port=0
    )
    server.port = server._httpd.server_address[1]  # noqa: SLF001
    server.start()
    try:
        page = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert "J.A.R.V.I.S." in page

        req = urllib.request.Request(
            server.url + "api/activate",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        body = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert body == {"ok": True, "result": {"started": True}} and calls == [{}]

        hub.publish("state", state="reposo", label="x")
        got = {}

        def _read() -> None:
            with urllib.request.urlopen(server.url + "events", timeout=5) as resp:
                for raw in resp:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        got.update(json.loads(line[6:]))
                        return

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(5)
        assert got == {"type": "state", "state": "reposo", "label": "x"}
    finally:
        server.stop()


def test_server_rejects_cross_origin_posts() -> None:
    server = HudServer(EventHub(), {"stop": lambda p: {}}, port=0)
    server.port = server._httpd.server_address[1]  # noqa: SLF001
    server.start()
    try:
        req = urllib.request.Request(
            server.url + "api/stop",
            data=b"{}",
            method="POST",
            headers={"Origin": "https://evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403
    finally:
        server.stop()


# ── hearing / brain selection ───────────────────────────────────────────────


def test_whisper_model_choice() -> None:
    from openjarvis.hud.voice import choose_whisper_model

    assert choose_whisper_model("auto", cuda=True) == (
        "large-v3-turbo",
        "cuda",
        "float16",
    )
    assert choose_whisper_model("auto", cuda=False) == ("medium", "cpu", "int8")
    assert choose_whisper_model("small", cuda=False)[0] == "small"


def test_cloud_model_detection() -> None:
    from openjarvis.hud.brain import is_cloud_model

    assert is_cloud_model("claude-sonnet-5-5")
    assert is_cloud_model("gpt-5")
    assert is_cloud_model("gemini-3-flash")
    assert not is_cloud_model("qwen3:8b")
    assert not is_cloud_model("")


def test_old_config_gets_new_sections(tmp_path) -> None:
    from openjarvis.hud.config import load_hud_config

    path = tmp_path / "jarvis.toml"
    path.write_text('[usuario]\ntratamiento = "jefe"\n', encoding="utf-8")
    cfg = load_hud_config(path)
    text = path.read_text(encoding="utf-8")
    assert cfg.honorific == "jefe"
    assert "[oido]" in text and "[cerebro]" in text
    assert text.count("[usuario]") == 1
    assert load_hud_config(path).modelo_oido == "auto"


def test_pick_installed_model() -> None:
    from openjarvis.hud.brain import pick_installed_model

    installed = ["nomic-embed-text:latest", "qwen3:8b"]
    assert pick_installed_model("qwen3.5:2b", installed) == "qwen3:8b"
    assert pick_installed_model("qwen3:8b", installed) == "qwen3:8b"
    assert pick_installed_model("llama3", ["llama3:latest"]) == "llama3:latest"
    assert pick_installed_model("qwen3:8b", []) == ""


def _endpoint(rms_values: list) -> tuple:
    from openjarvis.hud.voice import EndpointDetector

    det = EndpointDetector(end_silence=0.8, startup_timeout=6.0)
    for i, r in enumerate(rms_values):
        verdict = det.feed(r)
        if verdict in ("end", "timeout"):
            return verdict, round(i * det.BLOCK, 2)
    return "running", None


def test_endpoint_ends_even_when_agc_raises_background() -> None:
    # 0.3 s calibration, 0.3 s quiet, 1.5 s speech, then AGC-boosted noise.
    rms = [0.01] * 20 + [0.2] * 50 + [0.035] * 200
    verdict, t = _endpoint(rms)
    assert verdict == "end"
    assert t is not None and t < 3.2  # ~0.8 s after speech, not the 12 s cap


def test_endpoint_times_out_on_silence() -> None:
    assert _endpoint([0.01] * 400)[0] == "timeout"


def test_endpoint_caps_never_ending_speech() -> None:
    verdict, t = _endpoint([0.01] * 10 + [0.3] * 1000)
    assert verdict == "end" and t <= 12.5


def test_engine_choice(monkeypatch) -> None:
    from openjarvis.hud import brain as brain_mod
    from openjarvis.hud.config import HudConfig

    monkeypatch.setattr(brain_mod, "ollama_has_models", lambda host="": True)
    b = brain_mod.JarvisBrain(HudConfig())
    assert b._engine_for(None) == "ollama"
    assert b._engine_for("claude-sonnet-5-5") == "cloud"
    monkeypatch.setattr(brain_mod, "ollama_has_models", lambda host="": False)
    assert b._engine_for(None) is None
    assert (
        brain_mod.JarvisBrain(HudConfig(motor_cerebro="vllm"))._engine_for(None)
        == "vllm"
    )
