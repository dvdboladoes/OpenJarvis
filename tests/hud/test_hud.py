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
    assert choose_whisper_model("auto", cuda=False, ram_gb=8)[0] == "small"
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
    assert verdict == "end" and t <= 15.5


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


def test_sentence_splitter_streams_and_drops_thinking() -> None:
    from openjarvis.hud.brain import SentenceSplitter

    sp = SentenceSplitter()
    out: list = []
    for tok in [
        "<think>",
        "plan</think>",
        "Buenas ",
        "tardes, señor. Hoy ",
        "tiene **tres** reu",
        "niones. ¿Algo",
        " más?",
    ]:
        out += sp.feed(tok)
    out += sp.flush()
    assert out == ["Buenas tardes, señor.", "Hoy tiene tres reuniones.", "¿Algo más?"]


class _FakeSDK:
    class config:  # noqa: N801
        class intelligence:  # noqa: N801
            default_model = "fake"

    def _ensure_engine(self) -> None:
        pass

    def chat_stream(self, messages, **kw):  # type: ignore[no-untyped-def]
        self.last_messages = messages
        yield from ["Hola, ", "señor. ", "Todo en ", "orden."]


def test_assistant_speaks_while_streaming() -> None:
    from openjarvis.hud.assistant import JarvisAssistant
    from openjarvis.hud.config import HudConfig

    a = JarvisAssistant(
        HudConfig(puerto=0, fuentes=[], incluir_tiempo=False), mic=False
    )
    a.brain._sdk = _FakeSDK()  # noqa: SLF001
    a.brain._model = "fake"  # noqa: SLF001
    events: list = []
    a.hub.publish = lambda kind, **d: events.append((kind, d))  # type: ignore[method-assign]
    played: list = []
    a.speaker.synthesize = lambda text: text.encode()  # type: ignore[method-assign]
    a.speaker.play = lambda audio, on_level=None: played.append(audio.decode())  # type: ignore[method-assign]

    assert a._say_stream(a.brain.stream_reply("hola"), fallback=lambda: "x")  # noqa: SLF001
    assert played == ["Hola, señor.", "Todo en orden."]
    partials = [d["text"] for k, d in events if k == "partial"]
    assert partials == ["Hola, señor.", "Hola, señor. Todo en orden."]
    assert a.brain._history[-1] == ("assistant", "Hola, señor. Todo en orden.")  # noqa: SLF001
    a.server._httpd.server_close()  # noqa: SLF001 - never started


def test_assistant_falls_back_when_engine_fails() -> None:
    from openjarvis.hud.assistant import JarvisAssistant
    from openjarvis.hud.config import HudConfig

    a = JarvisAssistant(
        HudConfig(puerto=0, fuentes=[], incluir_tiempo=False), mic=False
    )

    def broken():  # type: ignore[no-untyped-def]
        raise RuntimeError("No inference engine available")
        yield  # pragma: no cover

    said: list = []
    a._say = lambda text: said.append(text)  # type: ignore[method-assign]  # noqa: SLF001
    notices: list = []
    a.hub.publish = lambda kind, **d: notices.append(d) if kind == "notice" else None  # type: ignore[method-assign]
    assert not a._say_stream(broken(), fallback=lambda: "modo de emergencia")  # noqa: SLF001
    assert said == ["modo de emergencia"]
    assert notices and "Ollama" in notices[0]["text"]
    a.server._httpd.server_close()  # noqa: SLF001 - never started


# ── v2: emotions, skills, settings ─────────────────────────────────────────


def test_splitter_reads_emotion_tags() -> None:
    from openjarvis.hud.brain import SentenceSplitter

    sp = SentenceSplitter()
    out = sp.feed("[bromista] ¡Buon giorno, señor! Vaya energía. [serio] Ahora bien, ")
    out += sp.feed("tenemos tres reuniones.") + sp.flush()
    assert [(str(o), o.mood) for o in out] == [
        ("¡Buon giorno, señor!", "bromista"),
        ("Vaya energía.", "bromista"),
        ("Ahora bien, tenemos tres reuniones.", "serio"),
    ]


def test_prosody_follows_mood() -> None:
    from openjarvis.hud.voice import prosody_for

    fast, pitch, vol = prosody_for("entusiasta", 1.0)
    slow, low, _ = prosody_for("empatico", 1.0)
    assert fast > 1.0 > slow
    assert pitch.startswith("+") and low.startswith("-")
    assert prosody_for("desconocido") == (1.0, "+0Hz", "+0%")


def test_skill_parsers() -> None:
    from openjarvis.hud.skills import parse_note, parse_timer

    assert parse_timer("pon un temporizador de 5 minutos") == (300, "5 minutos")
    assert parse_timer("alarma en diez segundos") == (10, "10 segundos")
    assert parse_timer("¿qué hora es?") is None
    assert (
        parse_note("apunta que tengo que llamar a Marta") == "Tengo que llamar a Marta"
    )
    assert parse_note("hola jarvis") is None


def test_notes_store_roundtrip(tmp_path) -> None:
    from openjarvis.hud.skills import NotesStore

    store = NotesStore(tmp_path / "notas.json")
    note = store.add("Comprar café")
    assert NotesStore(tmp_path / "notas.json").texts() == ["Comprar café"]
    assert store.delete(note["id"]) and store.texts() == []


def test_memory_persists_between_sessions(tmp_path) -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    path = str(tmp_path / "mem.json")
    b = JarvisBrain(HudConfig(), memory_path=path)
    b._remember("hola", "¡Hola, señor!")  # noqa: SLF001
    again = JarvisBrain(HudConfig(), memory_path=path)
    assert again._history[-2:] == [("user", "hola"), ("assistant", "¡Hola, señor!")]  # noqa: SLF001
    again.forget()
    assert JarvisBrain(HudConfig(), memory_path=path)._history == []  # noqa: SLF001


def test_save_config_value(tmp_path) -> None:
    from openjarvis.hud.config import (
        DEFAULT_TEMPLATE,
        load_hud_config,
        save_config_value,
    )

    p = tmp_path / "jarvis.toml"
    p.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    save_config_value(p, "voz", "voz", "en-US-AndrewMultilingualNeural")
    cfg = load_hud_config(p)
    assert cfg.voz == "en-US-AndrewMultilingualNeural"
    assert cfg.motor_oido == "local"  # other sections untouched


def test_lite_mode_for_local_models() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    local = JarvisBrain(HudConfig(), memory_path="/nonexistent/mem.json")
    local._model = "qwen3:8b"  # noqa: SLF001
    assert local.lite
    assert local._engine_options()["num_ctx"] == 3072  # noqa: SLF001
    assert local._token_budget(500) == 220  # noqa: SLF001
    assert len(local.system_prompt()) < 2500

    cloud = JarvisBrain(
        HudConfig(modelo="claude-sonnet-5-5"), memory_path="/nonexistent/m.json"
    )
    cloud._model = "claude-sonnet-5-5"  # noqa: SLF001
    assert not cloud.lite and cloud._engine_options() == {}  # noqa: SLF001

    full = JarvisBrain(
        HudConfig(modo_cerebro="completo"), memory_path="/nonexistent/m.json"
    )
    full._model = "qwen3:8b"  # noqa: SLF001
    assert not full.lite
    assert full._engine_options()["num_ctx"] == 8192  # noqa: SLF001


def test_interrupt_frees_a_busy_session() -> None:
    import threading as _t

    from openjarvis.hud.assistant import JarvisAssistant
    from openjarvis.hud.brain import Spoken
    from openjarvis.hud.config import HudConfig

    a = JarvisAssistant(
        HudConfig(puerto=0, fuentes=[], incluir_tiempo=False), mic=False
    )
    a.speaker.synthesize = lambda text, mood="": b"x"  # type: ignore[method-assign]
    a.speaker.play = lambda audio, on_level=None: None  # type: ignore[method-assign]
    gate = _t.Event()

    def slow_model():  # type: ignore[no-untyped-def]
        yield Spoken("Primera frase.", "serio")
        gate.wait(30)  # a model that takes "forever" to write the rest
        yield Spoken("Nunca llega.", "serio")

    assert a._begin()  # noqa: SLF001
    worker = _t.Thread(
        target=lambda: (a._say_stream(slow_model(), lambda: ""), a._end())
    )  # noqa: SLF001
    worker.start()
    import time as _time

    _time.sleep(0.3)
    assert a._session.locked()  # noqa: SLF001
    assert a.interrupt(timeout=3.0)  # returns quickly instead of waiting 30 s
    gate.set()
    worker.join(2)
    a.timers.stop()
    a.server._httpd.server_close()  # noqa: SLF001


def test_stream_recovers_from_model_loop() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    calls: list = []

    class LoopingSDK:
        class config:  # noqa: N801
            class intelligence:  # noqa: N801
                default_model = "qwen3:1.7b"

        def _ensure_engine(self) -> None:
            pass

        def chat_stream(self, messages, **kw):  # type: ignore[no-untyped-def]
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError(
                    "Ollama returned 500: prediction aborted, "
                    "token repeat limit reached"
                )
            yield from ["[alegre] ¡Hola, ", "señor!"]

    b = JarvisBrain(HudConfig(), memory_path="/nonexistent/mem.json")
    b._sdk = LoopingSDK()  # noqa: SLF001
    b._model = "qwen3:1.7b"  # noqa: SLF001
    out = list(b.stream_reply("hola"))
    assert [str(s) for s in out] == ["¡Hola, señor!"]
    assert calls[1]["repeat_penalty"] > calls[0]["repeat_penalty"]
    assert "/no_think" not in b.system_prompt()


def test_complete_falls_back_to_thinking_mode() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    seen: list = []

    class SDK:
        class config:  # noqa: N801
            class intelligence:  # noqa: N801
                default_model = "qwen3:1.7b"

        def _ensure_engine(self) -> None:
            pass

        def chat(self, messages, **kw):  # type: ignore[no-untyped-def]
            seen.append((len(messages), kw))
            if "think" not in kw:
                raise RuntimeError("prediction aborted, token repeat limit reached")
            return "<think>hmm</think>[sereno] Sistemas en línea."

    b = JarvisBrain(HudConfig(), memory_path="/nonexistent/mem.json")
    b._history = [("user", "x"), ("assistant", "y")]  # noqa: SLF001
    b._sdk = SDK()  # noqa: SLF001
    b._model = "qwen3:1.7b"  # noqa: SLF001
    assert b.reply("estado") == "Sistemas en línea."
    assert len(seen) == 3
    assert seen[0][0] > seen[1][0]  # retries drop the history
    assert seen[2][1]["think"] is None and seen[2][1]["max_tokens"] >= 700


def test_cpu_only_option() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig, parse_hud_config

    assert parse_hud_config({"cerebro": {"usar_gpu": False}}).usar_gpu is False
    b = JarvisBrain(HudConfig(usar_gpu=False), memory_path="/nonexistent/m.json")
    b._model = "qwen3:1.7b"  # noqa: SLF001
    assert b._engine_options()["num_gpu"] == 0  # noqa: SLF001
    b2 = JarvisBrain(HudConfig(), memory_path="/nonexistent/m.json")
    b2._model = "qwen3:1.7b"  # noqa: SLF001
    assert "num_gpu" not in b2._engine_options()  # noqa: SLF001


def test_endpoint_keeps_speech_that_starts_immediately() -> None:
    # The user talks straight away: calibration blocks are already speech.
    rms = [0.15, 0.2, 0.05, 0.18, 0.22, 0.01, 0.2, 0.19, 0.17, 0.2] + [0.2] * 40
    rms += [0.01] * 60
    verdict, t = _endpoint(rms)
    assert verdict == "end" and t is not None and t > 1.5


def test_hallucination_filter() -> None:
    from openjarvis.hud.voice import is_hallucination

    assert is_hallucination("Subtítulos realizados por la comunidad de Amara.org")
    assert is_hallucination("Gracias.")
    assert is_hallucination("")
    assert not is_hallucination("¿Qué tal estás, Jarvis?")


def test_system_prompt_is_stable_between_questions() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    b = JarvisBrain(HudConfig(), memory_path="/nonexistent/m.json")
    b._model = "qwen3:1.7b"  # noqa: SLF001
    p1 = b.system_prompt()
    import re as _re

    assert not _re.search(r"\b\d{1,2}:\d{2}\b", p1)  # no clock time → cacheable


def test_clean_history_drops_parroting() -> None:
    from openjarvis.hud.brain import clean_history

    brief = (
        "¡Buenos días! Hoy es lunes y la agenda tiene tareas. ¿En qué puedo ayudarte?"
    )
    hist = [
        ("assistant", brief),  # briefing: no question before it
        ("user", "hola"),
        ("assistant", brief),
        ("user", "¿qué tal?"),
        ("assistant", "¡Hola! " + brief),  # same reply again
        ("user", "cuéntame un chiste"),
        ("assistant", "¿Por qué el libro de mates estaba triste?"),
    ]
    out = clean_history(hist)
    assert out == [
        ("user", "hola"),
        ("assistant", brief),
        ("user", "cuéntame un chiste"),
        ("assistant", "¿Por qué el libro de mates estaba triste?"),
    ]


def test_stream_retries_when_model_copies_last_reply() -> None:
    from openjarvis.hud.brain import JarvisBrain
    from openjarvis.hud.config import HudConfig

    calls: list = []

    class SDK:
        class config:  # noqa: N801
            class intelligence:  # noqa: N801
                default_model = "qwen3:1.7b"

        def _ensure_engine(self) -> None:
            pass

        def chat_stream(self, messages, **kw):  # type: ignore[no-untyped-def]
            calls.append(len(messages))
            if len(calls) == 1:
                yield from ["Buenos días, hoy es lunes y todo va bien. ", "Más."]
            else:
                yield "[alegre] ¡Muy bien, gracias por preguntar!"

    b = JarvisBrain(HudConfig(), memory_path="/nonexistent/m.json")
    b._history = [
        ("user", "hola"),
        ("assistant", "Buenos días, hoy es lunes y todo va bien."),
    ]  # noqa: SLF001
    b._sdk = SDK()  # noqa: SLF001
    b._model = "qwen3:1.7b"  # noqa: SLF001
    out = [str(x) for x in b.stream_reply("¿qué tal estás?")]
    assert out == ["¡Muy bien, gracias por preguntar!"]
    assert calls[0] > calls[1]  # second try without history


# ── continuous listening ───────────────────────────────────────────────────


def _segment(rms_values: list, **kw):  # type: ignore[no-untyped-def]
    from openjarvis.hud.listening import SpeechSegmenter

    seg = SpeechSegmenter(**kw)
    out = []
    for i, r in enumerate(rms_values):
        frames = seg.feed(r, i)
        if frames:
            out.append((frames[0], frames[-1], len(frames)))
    return out


def test_segmenter_finds_each_sentence() -> None:
    noise = [0.01] * 40
    speech = [0.15, 0.2, 0.12, 0.18] * 15  # ~1.8 s
    utts = _segment(noise + speech + [0.01] * 40 + speech + [0.01] * 40)
    assert len(utts) == 2
    first_block, last_block, n = utts[0]
    assert first_block < 40 <= last_block  # pre-roll keeps the first syllable
    assert n * 0.03 >= 1.8


def test_segmenter_ignores_clicks_and_noise() -> None:
    clicks = ([0.01] * 20 + [0.3] + [0.01] * 20) * 5  # keyboard taps
    assert _segment([0.01] * 40 + clicks) == []
    hum = [0.04] * 400  # steady fan noise is learned as the floor
    assert _segment(hum) == []


def test_segmenter_handles_laptop_agc() -> None:
    # After you stop talking the mic raises its gain: background goes up.
    rms = [0.01] * 40 + [0.25] * 50 + [0.04] * 60
    assert len(_segment(rms)) == 1


def test_continuous_mode_answers_each_utterance() -> None:
    import time as _time

    import numpy as _np

    from openjarvis.hud.assistant import JarvisAssistant
    from openjarvis.hud.brain import Spoken
    from openjarvis.hud.config import HudConfig

    a = JarvisAssistant(
        HudConfig(puerto=0, fuentes=[], incluir_tiempo=False), mic=False
    )

    class FakeListener:
        detectors = ["continua"]
        resumed = 0

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            FakeListener.resumed += 1

    a.listener = FakeListener()
    a.ear._resolve = lambda: True  # type: ignore[method-assign]  # noqa: SLF001
    a.ear.transcribe = lambda audio: "¿qué tal, Jarvis?"  # type: ignore[method-assign]
    a._get_snapshot = lambda force=False: None  # type: ignore[method-assign]  # noqa: SLF001
    said: list = []

    def fake_reply(text):  # type: ignore[no-untyped-def]
        yield Spoken("¡Muy bien, señor!", "alegre")

    a.brain.stream_reply = fake_reply  # type: ignore[method-assign]
    a.speaker.synthesize = lambda text, mood="": b"x"  # type: ignore[method-assign]
    a.speaker.play = lambda audio, on_level=None: said.append("play")  # type: ignore[method-assign]
    a._on_utterance(_np.zeros(16000, dtype=_np.float32))  # noqa: SLF001
    for _ in range(50):
        _time.sleep(0.05)
        if FakeListener.resumed:
            break
    assert said == ["play"] and FakeListener.resumed == 1
    a.timers.stop()
    a.server._httpd.server_close()  # noqa: SLF001
