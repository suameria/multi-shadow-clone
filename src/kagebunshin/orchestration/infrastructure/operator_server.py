"""Loopback-only operator transport with fixed routes and same-origin actions."""

from http.server import ThreadingHTTPServer
from pathlib import Path
import secrets

from ..presentation.operator_http import Handler


class OperatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, operator, mode: str, port: int = 0):
        self.operator, self.mode = operator, mode
        self.csrf = secrets.token_urlsafe(32)
        root = Path(__file__).resolve().parent.parent / "presentation"
        self.assets = {"/": ((root / "operator.html").read_text().replace("__CSRF_TOKEN__", self.csrf), "text/html; charset=utf-8"),
                       "/operator.js": ((root / "operator.js").read_text(), "text/javascript; charset=utf-8"),
                       "/operator.css": ((root / "operator.css").read_text(), "text/css; charset=utf-8")}
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
