"""Serve the app window on 127.0.0.1 and open it.

Only this Mac can connect, and every request must carry a random token that is
part of the URL iGround opens, so other web pages can't drive it.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .service import Service, ServiceError

INDEX = Path(__file__).parent / "static" / "index.html"
CHROME = Path("/Applications/Google Chrome.app")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: Service, port: int = 0, token: Optional[str] = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.service = service
        self.token = token or secrets.token_urlsafe(24)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    # -- plumbing ----------------------------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _authorised(self) -> bool:
        return self._host_ok() and secrets.compare_digest(
            self.headers.get("X-IGround-Token", ""), self.server.token)

    # -- routes ------------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            token = parse_qs(url.query).get("t", [""])[0]
            if not self._host_ok() or not secrets.compare_digest(token, self.server.token):
                self._send(403, b"Open iGround by double-clicking iGround.command.", "text/plain; charset=utf-8")
                return
            page = INDEX.read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            if not self._authorised():
                self._json(403, {"error": "forbidden"})
                return
            self._json(200, self.server.service.state())
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        if not self._authorised():
            self._json(403, {"error": "forbidden"})
            return
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        s = self.server.service
        actions: dict = {
            "/api/backup": lambda: s.start_backup(new=bool(body.get("new"))),
            "/api/verify": s.start_verify,
            "/api/stop": s.stop,
            "/api/dismiss": s.dismiss,
            "/api/refresh": s.refresh,
            "/api/drive": lambda: s.select_drive(str(body.get("path", ""))),
            "/api/settings": lambda: s.update_settings(body.get("kinds"), body.get("evict"), body.get("skip"),
                                                    body.get("tip_seen")),
            "/api/iphone": lambda: s.iphone(str(body.get("action", ""))),
            "/api/open": lambda: s.open(str(body.get("target", ""))),
        }
        action: Optional[Callable] = actions.get(urlparse(self.path).path)
        if action is None:
            self._json(404, {"error": "not found"})
            return
        try:
            action()
        except ServiceError as exc:
            self._json(409, {"error": str(exc)})
            return
        self._json(200, s.state())


def open_window(url: str, run: Callable = subprocess.run) -> bool:
    """Open the app: a clean app-style window in Chrome if installed, else the default browser."""
    if not shutil.which("open"):
        return False
    if CHROME.exists():
        run(["open", "-na", str(CHROME), "--args", f"--app={url}", "--window-size=560,820"], capture_output=True)
    else:
        run(["open", url], capture_output=True)
    return True


def serve(port: int = 0, open_browser: bool = True, service: Optional[Service] = None) -> None:
    server = AppServer(service or Service(), port)
    opened = open_browser and open_window(server.url)
    print("iGround is running." + (" Your browser should open in a moment." if opened else ""))
    print(f"\n  {server.url}\n")
    print("Keep this window open while iGround is copying. Close it (or press Ctrl-C) to quit.")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
