"""HTTP validation and view projection for the owned operator use cases."""
from http.server import BaseHTTPRequestHandler
import json
import secrets
from ..ports import Conflict


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate action field")
        result[key] = value
    return result


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def _host(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def _reply(self, status, data, content_type="application/json; charset=utf-8"):
        payload = (json.dumps(data, ensure_ascii=False) if content_type.startswith("application/json") else data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self._host():
            return self._reply(403, {"error": "host rejected"})
        if self.path in self.server.assets:
            body, content_type = self.server.assets[self.path]
            return self._reply(200, body, content_type)
        if self.path == "/api/settings":
            return self._reply(200, self.server.operator.settings_snapshot())
        if self.path == "/api/state":
            return self._reply(200, {**self.server.operator.snapshot(), "mode": self.server.mode})
        return self._reply(404, {"error": "unknown route"})

    def do_POST(self):
        if (not self._host() or self.headers.get("Origin") != self.server.origin
            or not secrets.compare_digest(self.headers.get("X-Kagebunshin-CSRF", ""), self.server.csrf)):
            return self._reply(403, {"error": "same-origin operator action required"})
        if self.path not in {"/api/action", "/api/requests", "/api/settings", "/api/usage"} or self.headers.get_content_type() != "application/json":
            return self._reply(400, {"error": "unsupported operator request"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= (60000 if self.path in {"/api/requests", "/api/settings"} else 2048):
                raise ValueError("request size rejected")
            raw = json.loads(self.rfile.read(length), object_pairs_hook=unique_object)
            if self.path == "/api/usage":
                if raw != {}:
                    raise ValueError("usage refresh expects an empty object")
                result = self.server.operator.refresh_usage()
            elif self.path == "/api/settings":
                if not isinstance(raw, dict) or set(raw) != {"value", "expected_revision"}:
                    raise ValueError("exact value and expected_revision required")
                result = self.server.operator.save_settings(**raw)
            elif self.path == "/api/requests":
                if not isinstance(raw, dict) or set(raw) != {"objective", "source", "max_turns"}:
                    raise ValueError("exact objective, source and max_turns required")
                result = self.server.operator.submit(**raw)
            else:
                if not isinstance(raw, dict) or set(raw) != {"operation", "run_id"}:
                    raise ValueError("exact operation and run_id required")
                if any(not isinstance(v, str) or len(v) > 100 for v in raw.values()):
                    raise ValueError("invalid action fields")
                result = self.server.operator.act(raw["operation"], raw["run_id"])
        except (ValueError, KeyError, Conflict) as exc:
            return self._reply(409, {"error": str(exc)})
        return self._reply(200, result)
