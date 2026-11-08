"""Tests for the tool-using Jarvis: memory, tools, confirmations, remote PIN."""

from __future__ import annotations

import urllib.request
from types import SimpleNamespace
from typing import Any, List

import pytest

from openjarvis.hud.agent import AWAIT_REPLY, ClaudeAgent, is_yes
from openjarvis.hud.brain import Spoken
from openjarvis.hud.computer import key_combo, scale_factor
from openjarvis.hud.config import SETTINGS, HudConfig, coerce_setting
from openjarvis.hud.memory import MemoryStore
from openjarvis.hud.server import EventHub, HudServer
from openjarvis.hud.window import is_wake_phrase

# ── memory ──────────────────────────────────────────────────────────────────


def test_memory_persists_dedupes_and_forgets(tmp_path) -> None:
    path = tmp_path / "recuerdos.json"
    mem = MemoryStore(path)
    first = mem.add("Su canción favorita es Bohemian Rhapsody, de Queen")
    dup = mem.add("su  canción favorita es bohemian rhapsody, de queen")
    assert dup["id"] == first["id"]
    mem.add("Vive en Irún")
    again = MemoryStore(path)  # survives a restart
    assert [m["texto"] for m in again.list()] == [
        "Su canción favorita es Bohemian Rhapsody, de Queen",
        "Vive en Irún",
    ]
    assert "Bohemian Rhapsody" in again.as_prompt()
    assert again.forget_matching("canción favorita") == 1
    assert again.update(again.list()[0]["id"], "Vive en San Sebastián")
    assert MemoryStore(path).list()[0]["texto"] == "Vive en San Sebastián"
    with pytest.raises(ValueError):
        again.add("   ")


# ── words ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Jarvis, enciéndete",
        "jarvis enciendete por favor",
        "Oye Jarvis despierta",
        "enciéndete Jarvis",
        "Yarvis, enciéndete",
    ],
)
def test_wake_phrase(text: str) -> None:
    assert is_wake_phrase(text)


@pytest.mark.parametrize("text", ["Jarvis, ¿qué tiempo hace?", "enciende la luz", ""])
def test_not_wake_phrase(text: str) -> None:
    assert not is_wake_phrase(text)


@pytest.mark.parametrize(
    "text", ["Sí", "sí, envíalo", "Vale", "dale", "adelante, hazlo"]
)
def test_yes(text: str) -> None:
    assert is_yes(text)


@pytest.mark.parametrize("text", ["No", "no, espera", "cancela", "mejor no", "", "mmm"])
def test_not_yes(text: str) -> None:
    assert not is_yes(text)


def test_key_names_and_scaling() -> None:
    assert key_combo("ctrl+shift+Tab") == ["ctrl", "shift", "tab"]
    assert key_combo("Return") == ["enter"]
    assert key_combo("super") == ["win"]
    assert key_combo("F5") == ["f5"]
    assert scale_factor(1280, 720) == 1.0
    assert scale_factor(3840, 2160) == pytest.approx(0.5, abs=0.01)


def test_settings_validation() -> None:
    spec = {s["clave"]: s for s in SETTINGS}
    assert coerce_setting(spec["silencio"], "9") == 1.5
    assert coerce_setting(spec["tono"], 3.7) == 3
    assert coerce_setting(spec["pensar"], 1) is True
    with pytest.raises(ValueError):
        coerce_setting(spec["modelo"], "gpt-5")
    cfg = HudConfig()
    for s in SETTINGS:  # every setting maps to a real config value
        if s["attr"].startswith("gesto:"):
            assert s["attr"].split(":", 1)[1] in cfg.gestos
        else:
            assert hasattr(cfg, s["attr"]), s["attr"]


# ── the agent with a fake Claude ────────────────────────────────────────────


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


class _FakeStream:
    def __init__(self, texts: List[str], blocks: List[_Block], stop: str) -> None:
        self.texts, self.blocks, self.stop = texts, blocks, stop

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def __iter__(self):
        start = _Block(type="text")
        yield SimpleNamespace(type="content_block_start", content_block=start)
        for t in self.texts:
            yield SimpleNamespace(type="text", text=t)

    def get_final_message(self):
        return SimpleNamespace(content=self.blocks, stop_reason=self.stop)


class _FakeClient:
    def __init__(self, turns: List[_FakeStream]) -> None:
        self.turns = turns
        self.requests: List[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **req: Any) -> _FakeStream:
        self.requests.append(req)
        return self.turns.pop(0)


def _agent(tmp_path, turns, **kw) -> tuple:
    brain = SimpleNamespace(_history=[], MAX_HISTORY=24, _facts="", _notes="")
    saved: list = []
    brain._remember = lambda u, r: saved.append((u, r))
    opened: list = []
    agent = ClaudeAgent(
        HudConfig(modelo="claude-haiku-5-5"),
        brain,
        MemoryStore(tmp_path / "m.json"),
        open_url=opened.append,
        **kw,
    )
    agent._client = _FakeClient(turns)  # noqa: SLF001
    return agent, opened, saved


def test_fire_and_forget_tool_ends_the_turn(tmp_path) -> None:
    call = _Block(
        type="tool_use",
        id="t1",
        name="abrir_web",
        input={"url": "dashboard.stripe.com"},
    )
    turn = _FakeStream(
        ["Ahora mismo se lo abro, señor. "],
        [_Block(type="text", text="Ahora mismo…"), call],
        "tool_use",
    )
    agent, opened, saved = _agent(tmp_path, [turn])
    said = [str(s) for s in agent.stream("abre Stripe")]
    assert said == ["Ahora mismo se lo abro,", "señor."]
    assert opened == ["https://dashboard.stripe.com"]
    assert len(agent.client.requests) == 1  # no second round trip
    req = agent.client.requests[0]
    assert req["extra_body"]["thinking"] == {"type": "disabled"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any(t.get("type") == "web_search_20250305" for t in req["tools"])
    # The history also records what was done (not spoken), for later turns.
    assert saved == [
        (
            "abre Stripe",
            "Ahora mismo se lo abro, señor. [Acciones hechas: "
            "abrir_web: Abierta https://dashboard.stripe.com]",
        )
    ]


def test_memory_tool_and_silent_call_says_done(tmp_path) -> None:
    call = _Block(
        type="tool_use",
        id="t1",
        name="recordar",
        input={"dato": "Su comida favorita es la paella"},
    )
    agent, _, _ = _agent(tmp_path, [_FakeStream([], [call], "tool_use")])
    said = [str(s) for s in agent.stream("mi comida favorita es la paella")]
    assert said == ["Hecho, señor."]
    assert agent.memory.list()[0]["texto"] == "Su comida favorita es la paella"
    assert "paella" in agent.system_prompt()


def test_email_needs_a_spoken_yes(tmp_path, monkeypatch) -> None:
    from openjarvis.hud import winctl

    sent: list = []
    monkeypatch.setattr(
        winctl, "send_open_email", lambda: sent.append(1) or "Correo enviado."
    )
    call = _Block(
        type="tool_use",
        id="t1",
        name="redactar_correo",
        input={"para": "ana@example.com", "asunto": "Hola", "cuerpo": "¿Qué tal?"},
    )

    def turns(reply: str) -> list:
        return [
            _FakeStream([], [call], "tool_use"),
            _FakeStream([reply], [_Block(type="text", text="x")], "end_turn"),
        ]

    agent, opened, _ = _agent(
        tmp_path, turns("No lo he enviado. "), get_reply=lambda _t: "no, espera"
    )
    out = list(agent.stream("manda un correo a Ana"))
    assert AWAIT_REPLY in out
    assert any("¿Lo envío?" in str(s) for s in out if isinstance(s, Spoken))
    assert sent == []
    assert opened and opened[0].startswith("https://mail.google.com/")
    results = [
        block
        for msg in agent.client.requests[1]["messages"]
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if block.get("type") == "tool_result"
    ]
    assert "NO ha confirmado" in results[0]["content"]

    agent2, _, _ = _agent(
        tmp_path, turns("Enviado. "), get_reply=lambda _t: "sí, envíalo"
    )
    list(agent2.stream("manda un correo a Ana"))
    assert sent == [1]


# ── remote access needs the PIN ─────────────────────────────────────────────


def test_remote_access_requires_pin() -> None:
    server = HudServer(
        EventHub(), {"stop": lambda p: {}}, port=0, remote=True, pin="4321"
    )
    assert server.remote
    port = server._httpd.server_address[1]  # noqa: SLF001
    server.start()
    try:
        handler = server._httpd.RequestHandlerClass  # noqa: SLF001
        stranger = SimpleNamespace(client_address=("192.168.1.50", 5000))
        assert not handler.is_local.fget(stranger)
        # From this PC it works without a PIN.
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        assert b"J.A.R.V.I.S." in page
    finally:
        server.stop()
    assert not HudServer(EventHub(), {}, port=0, remote=True, pin="").remote


def test_events_hub_routes_audio_only_to_remote_clients() -> None:
    hub = EventHub()
    local, remote = hub.subscribe(), hub.subscribe(remote=True)
    hub.publish("audio", mp3="AAA")
    hub.publish("state", state="reposo", label="")
    assert remote.get_nowait()["type"] == "audio"
    assert local.get_nowait()["type"] == "state"
    assert hub.local_clients == 1 and hub.remote_clients == 1


def test_you_can_finish_your_sentence() -> None:
    """Speech that arrives while Jarvis is still thinking joins the question."""
    import time

    from openjarvis.hud.assistant import JarvisAssistant

    cfg = HudConfig(
        puerto=0, fuentes=[], incluir_tiempo=False, modelo="claude-haiku-5-5"
    )
    a = JarvisAssistant(cfg, mic=False)
    heard: list = []
    a._on_text = heard.append  # type: ignore[method-assign]  # noqa: SLF001
    a.interrupt = lambda timeout=5.0: True  # type: ignore[method-assign]
    try:
        a._session.acquire()  # noqa: SLF001
        a._state = "pensando"  # noqa: SLF001
        a._question = "pon música de"  # noqa: SLF001
        assert a._api_heard({"text": "los Beatles"})["unido"]  # noqa: SLF001
        for _ in range(50):
            if heard:
                break
            time.sleep(0.02)
        assert heard == ["pon música de los Beatles"]
        # Once Jarvis has started talking, new speech no longer interrupts.
        a._question, a._spoke = "hola", True  # noqa: SLF001
        assert a._api_heard({"text": "otra cosa"}) == {"ok": False, "busy": True}  # noqa: SLF001
    finally:
        a._session.release()  # noqa: SLF001
        a.server._httpd.server_close()  # noqa: SLF001


def test_prepared_phrases_lose_their_silence() -> None:
    import numpy as np

    from openjarvis.hud.voice import trim_silence

    rate = 24000
    quiet = np.zeros(rate, dtype=np.float32)
    voice = np.full(rate // 2, 0.3, dtype=np.float32)
    out = trim_silence([quiet, voice, quiet], rate)
    assert len(out) < rate  # was 2.5 s, now ~0.6 s
    assert len(out) >= rate // 2


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Apágate", "background"),
        ("Jarvis, ponte en segundo plano", "background"),
        ("descansa", "background"),
        ("ciérrate del todo", "quit"),
        ("apaga la luz", None),
        ("¿Qué es el segundo plano en fotografía y cómo se usa en un retrato?", None),
    ],
)
def test_power_commands(text: str, want) -> None:
    from openjarvis.hud.brain import power_command

    assert power_command(text) == want


def test_own_echo_is_ignored() -> None:
    import time

    from openjarvis.hud.assistant import JarvisAssistant

    a = JarvisAssistant(
        HudConfig(puerto=0, fuentes=[], incluir_tiempo=False), mic=False
    )
    try:
        a._said_text = (
            "La Luna se formó tras el choque de un cuerpo del tamaño de Marte."  # noqa: SLF001
        )
        a._said_at = time.monotonic()  # noqa: SLF001
        assert a._is_own_echo("el choque de un cuerpo del tamaño")  # noqa: SLF001
        assert not a._is_own_echo("pon música de los Beatles")  # noqa: SLF001
        a._said_at -= 10  # noqa: SLF001
        assert not a._is_own_echo("el choque de un cuerpo del tamaño")  # noqa: SLF001
    finally:
        a.server._httpd.server_close()  # noqa: SLF001
