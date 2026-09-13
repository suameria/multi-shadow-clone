"""Private synthetic posts to an exact repository-owned local worktree only."""

import http.cookiejar
import json
import re
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from ..domain.model import Capabilities, InvalidDelivery, Receipt, fingerprint


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise InvalidDelivery("redirect is outside the owned loopback destination")


class GranSkypolisDestination:
    ACTOR = "local-smoke-author@example.com"

    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.environment = lifecycle.state()
        if not self.environment or self.environment.get("phase") != "running":
            raise InvalidDelivery("start the official worktree before constructing its destination")
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
        self.csrf = None
        self.base = lifecycle.endpoint(self.environment)

    def capabilities(self):
        # Replay is supported by the current service's payload fingerprint and
        # durable post binding; no separate lookup API is claimed.
        return Capabilities(self.environment["id"], self.ACTOR, lookup=False, idempotent_replay=True)

    def _request(self, method, path, payload=None, headers=None):
        if (method, path) not in {("GET", "/api/auth/csrf"), ("POST", "/api/auth/login"), ("POST", "/api/posts")}:
            raise InvalidDelivery("operation is outside the fixed local adapter")
        if self.lifecycle.endpoint(self.environment) != self.base:
            raise InvalidDelivery("destination changed")
        headers = {**(headers or {}), "Accept": "application/json", "Origin": self.base}
        if self.csrf:
            headers["X-CSRF-TOKEN"] = self.csrf
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=None if payload is None else json.dumps(payload).encode(),
                          headers=headers, method=method)
        with self.opener.open(request, timeout=20) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise InvalidDelivery("service response exceeds bound")
            return json.loads(raw)

    def login_fixture(self):
        # These are source-defined disposable seed credentials, never a real account.
        with self.lifecycle.locked():
            self.csrf = self._request("GET", "/api/auth/csrf")["csrf_token"]
            self._request("POST", "/api/auth/login", {"email": self.ACTOR, "password": "password"})
            self.csrf = self._request("GET", "/api/auth/csrf")["csrf_token"]

    def validate(self, payload):
        self.lifecycle.endpoint(self.environment)
        if (not isinstance(payload, dict) or set(payload) != {"kind", "visibility", "body"}
            or payload["kind"] != "tweet" or payload["visibility"] != "private"
            or not isinstance(payload["body"], str) or not 1 <= len(payload["body"].strip()) <= 5000
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", payload["body"])):
            raise InvalidDelivery("only bounded private synthetic text posts are supported")

    def send(self, payload, operation_key):
        self.validate(payload)
        if not self.csrf or not re.fullmatch(r"multi-shadow-clone-[a-f0-9]{64}", operation_key):
            raise InvalidDelivery("fixture login and bound operation key required")
        with self.lifecycle.locked():
            self.validate(payload)
            response = self._request("POST", "/api/posts", payload, {"Idempotency-Key": operation_key})
        post_id = response.get("data", {}).get("id")
        if not isinstance(post_id, str) or not post_id:
            raise InvalidDelivery("post outcome unknown: no remote identity")
        return Receipt(post_id, fingerprint(payload))

    def find(self, operation_key, payload_hash):
        return None
