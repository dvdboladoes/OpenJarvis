"""Tiny local web server for the HUD: static page + Server-Sent Events.

Standard library only, bound to 127.0.0.1. The page subscribes to
``/events`` and posts to ``/api/*`` for the on-screen buttons.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class EventHub:
    """Fan out JSON events to every connected HUD page.

    The latest value of each "sticky" event type is replayed to new clients so
    a page opened mid-conversation shows the current state immediately.
    """

    STICKY = {"state", "snapshot", "status", "transcript"}

    def __init__(self) -> None:
        self._clients: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._last: Dict[str, Dict[str, Any]] = {}
        self._log: List[Dict[str, Any]] = []

    def publish(self, kind: str, **data: Any) -> None:
        event = {"type": kind, **data}
        with self._lock:
            if kind in self.STICKY:
                self._last[kind] = event
            if kind == "message":
                self._log = (self._log + [event])[-30:]
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            for event in self._last.values():
                q.put_nowait(event)
            for event in self._log:
                q.put_nowait(event)
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)


def _read_static(name: str) -> Optional[bytes]:
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    try:
        return resources.files("openjarvis.hud").joinpath("static", name).read_bytes()
    except (FileNotFoundError, OSError):
        return None


def make_handler(
    hub: EventHub, actions: Dict[str, Callable[[Dict[str, Any]], Any]]
) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisHUD/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            logger.debug("hud: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
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

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            action = actions.get(path.removeprefix("/api/"))
            if not path.startswith("/api/") or action is None:
                self._send(404, b"{}", "application/json")
                return
            # Reject cross-site requests: only the HUD page itself may call us.
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if origin and origin.split("://", 1)[-1] != host:
                self._send(403, b"{}", "application/json")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 64_000)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
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
            q = hub.subscribe()
            try:
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
    ) -> None:
        self.hub = hub
        self.port = port
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", port), make_handler(hub, actions)
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
