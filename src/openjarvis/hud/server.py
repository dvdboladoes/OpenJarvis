"""Tiny web server for the HUD: static page + Server-Sent Events.

Standard library only. By default it listens on 127.0.0.1, so only this PC
can reach it. With remote access on (``[remoto] acceso = true``) it also
listens on the local network, and every device other than this PC must
enter the PIN first — that is how the Chromebook connects.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_LOCAL = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

PIN_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>J.A.R.V.I.S. · Acceso</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#03060c;
color:#cfefff;font:16px system-ui,sans-serif}
form{display:grid;gap:14px;padding:32px;border:1px solid #1d3b52;border-radius:18px;
background:rgba(10,30,48,.6);text-align:center;width:min(320px,86vw)}
h1{margin:0;font-size:20px;letter-spacing:.3em;color:#5fe3ff}
input{font-size:24px;text-align:center;padding:10px;border-radius:12px;
border:1px solid #2b5b7a;background:#06121e;color:#fff;letter-spacing:.3em}
button{padding:12px;border:0;border-radius:12px;background:#22c3ee;color:#02121c;
font-weight:700}
p{margin:0;color:#ff8a8a;min-height:1em}</style></head><body>
<form method="get" action="/"><h1>J.A.R.V.I.S.</h1>
<input name="pin" inputmode="numeric" autocomplete="off" placeholder="PIN" autofocus>
<button>Entrar</button><p>%s</p></form></body></html>"""


class EventHub:
    """Fan out JSON events to every connected HUD page.

    The latest value of each "sticky" event type is replayed to new clients so
    a page opened mid-conversation shows the current state immediately.
    """

    STICKY = {
        "state", "snapshot", "status", "transcript", "voices", "notes",
        "timers", "system", "mood", "mic", "listen", "screen_watch",
        "memory", "background", "routines", "reminders", "dictation",
    }  # fmt: skip

    def __init__(self) -> None:
        self._clients: List[queue.Queue] = []
        self._remote: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._last: Dict[str, Dict[str, Any]] = {}
        self._log: List[Dict[str, Any]] = []

    @property
    def remote_clients(self) -> int:
        with self._lock:
            return len(self._remote)

    @property
    def local_clients(self) -> int:
        with self._lock:
            return len(self._clients) - len(self._remote)

    def publish(self, kind: str, **data: Any) -> None:
        event = {"type": kind, **data}
        with self._lock:
            if kind in self.STICKY:
                self._last[kind] = event
            if kind == "message":
                self._log = (self._log + [event])[-30:]
            clients = list(self._remote if kind == "audio" else self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self, remote: bool = False) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            for event in self._last.values():
                q.put_nowait(event)
            for event in self._log:
                q.put_nowait(event)
            self._clients.append(q)
            if remote:
                self._remote.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)
            if q in self._remote:
                self._remote.remove(q)


def _read_static(name: str) -> Optional[bytes]:
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    try:
        return resources.files("openjarvis.hud").joinpath("static", name).read_bytes()
    except (FileNotFoundError, OSError):
        return None


def make_handler(
    hub: EventHub,
    actions: Dict[str, Callable[[Dict[str, Any]], Any]],
    server: Optional["HudServer"] = None,
) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisHUD/2.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            logger.debug("hud: " + fmt, *args)

        # -- auth ----------------------------------------------------------

        @property
        def is_local(self) -> bool:
            return self.client_address[0] in _LOCAL

        def _cookie_pin(self) -> str:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "jpin":
                    return value
            return ""

        def _authorized(self) -> bool:
            if self.is_local:
                return True
            pin = server.pin if server is not None else ""
            if not pin or not (server and server.remote):
                return False
            return hmac.compare_digest(self._cookie_pin(), pin)

        def _send(
            self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        # -- routes --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            if not self._authorized():
                self._login(parse_qs(query).get("pin", [""])[0])
                return
            if path == "/events":
                self._events()
                return
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            body = _read_static(name)
            if body is None:
                self._send(404, b"not found", "text/plain")
                return
            ext = "." + name.rsplit(".", 1)[-1] if "." in name else ".html"
            self._send(200, body, _STATIC_TYPES.get(ext, "application/octet-stream"))

        def _login(self, given: str) -> None:
            pin = server.pin if server is not None else ""
            if server is None or not server.remote or not pin:
                page = PIN_PAGE % "El acceso remoto está desactivado en este PC."
                self._send(403, page.encode(), "text/html; charset=utf-8")
                return
            if given and hmac.compare_digest(given, pin):
                self._send(
                    303,
                    b"",
                    "text/plain",
                    {
                        "Location": "/",
                        "Set-Cookie": f"jpin={pin}; Path=/; Max-Age=31536000; "
                        "HttpOnly; SameSite=Strict",
                    },
                )
                return
            msg = "PIN incorrecto." if given else ""
            self._send(401, (PIN_PAGE % msg).encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            action = actions.get(path.removeprefix("/api/"))
            if not path.startswith("/api/") or action is None:
                self._send(404, b"{}", "application/json")
                return
            if not self._authorized():
                self._send(401, b"{}", "application/json")
                return
            # Reject cross-site requests: only the HUD page itself may call us.
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if origin and origin.split("://", 1)[-1] != host:
                self._send(403, b"{}", "application/json")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 10_000_000)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:  # bad JSON or not UTF-8
                payload = {}
            try:
                result = action(payload if isinstance(payload, dict) else {})
                body = json.dumps({"ok": True, "result": result}).encode()
                self._send(200, body, "application/json")
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"ok": False, "error": str(exc)}).encode()
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, body, "application/json")

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe(remote=not self.is_local)
            try:
                hello = {"type": "hello", "local": self.is_local}
                self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
                while True:
                    try:
                        event = q.get(timeout=15)
                        line = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        line = ": ping\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


class HudServer:
    def __init__(
        self,
        hub: EventHub,
        actions: Dict[str, Callable[[Dict[str, Any]], Any]],
        port: int = 8765,
        *,
        remote: bool = False,
        pin: str = "",
    ) -> None:
        self.hub = hub
        self.port = port
        self.pin = pin
        # Remote access needs a PIN: without one we stay local-only.
        self.remote = bool(remote and pin)
        host = "0.0.0.0" if self.remote else "127.0.0.1"  # noqa: S104
        self._httpd = ThreadingHTTPServer(
            (host, port), make_handler(hub, actions, self)
        )
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="jarvis-hud-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


__all__ = ["EventHub", "HudServer", "make_handler"]
