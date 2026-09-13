from contextlib import nullcontext
from copy import deepcopy
import unittest

from multi_shadow_clone.delivery.domain.model import InvalidDelivery, fingerprint
from multi_shadow_clone.delivery.infrastructure.granskypolis import GranSkypolisDestination, NoRedirect


class FakeLifecycle:
    def __init__(self):
        self.current = {"id": "owned-environment", "phase": "running"}
    def state(self): return deepcopy(self.current)
    def endpoint(self, expected):
        if expected != self.current:
            raise InvalidDelivery("changed environment")
        return "http://127.0.0.1:12345"
    def locked(self): return nullcontext()


class GranSkypolisContractTest(unittest.TestCase):
    def test_only_private_text_posts_and_exact_environment_are_accepted(self):
        lifecycle = FakeLifecycle()
        destination = GranSkypolisDestination(lifecycle)
        payload = {"kind": "tweet", "visibility": "private", "body": "synthetic fixture"}
        destination.validate(payload)
        for bad in [{**payload, "visibility": "public"}, {**payload, "url": "https://elsewhere.test"},
                    {**payload, "body": "x" * 5001}, {**payload, "body": ""}]:
            with self.subTest(bad=list(bad)):
                with self.assertRaises(InvalidDelivery): destination.validate(bad)
        lifecycle.current["id"] = "another-environment"
        with self.assertRaises(InvalidDelivery): destination.validate(payload)

    def test_redirect_and_arbitrary_endpoint_are_rejected_without_network(self):
        destination = GranSkypolisDestination(FakeLifecycle())
        with self.assertRaises(InvalidDelivery): destination._request("POST", "/arbitrary")
        with self.assertRaises(InvalidDelivery): NoRedirect().redirect_request(None)

    def test_receipt_binds_sent_payload_and_service_idempotency_key(self):
        destination = GranSkypolisDestination(FakeLifecycle())
        destination.csrf = "fixture-csrf"
        requests = []
        def request(*args):
            requests.append(args)
            return {"data": {"id": "fixture-post"}}
        destination._request = request
        payload = {"kind": "tweet", "visibility": "private", "body": "synthetic fixture"}
        key = "multi-shadow-clone-" + "a" * 64
        receipt = destination.send(payload, key)
        self.assertEqual(receipt.payload_hash, fingerprint(payload))
        self.assertEqual(requests[0], ("POST", "/api/posts", payload, {"Idempotency-Key": key}))
        self.assertTrue(destination.capabilities().idempotent_replay)
        self.assertFalse(destination.capabilities().lookup)
