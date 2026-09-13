from copy import deepcopy
from dataclasses import replace
import unittest

from multi_shadow_clone.delivery.application.outbox import Outbox
from multi_shadow_clone.delivery.domain.model import Capabilities, InvalidDelivery, Receipt, fingerprint
from multi_shadow_clone.delivery.ports import Conflict


class MemoryDelivery:
    def __init__(self):
        self.ledger = {"revision": 0, "epoch": 0, "stopped": False, "operations": {}, "bindings": {}}
    def read(self):
        return deepcopy(self.ledger)
    def save(self, ledger, expected_revision):
        if self.ledger["revision"] != expected_revision:
            raise Conflict()
        self.ledger = deepcopy({**ledger, "revision": expected_revision + 1})


class AcceptedFixture:
    def __init__(self):
        self.payload = {"body": "synthetic post", "visibility": "private"}
        self.audited = True
    def accepted_result(self, run_id, node_id):
        return {"audit_role": "R12" if self.audited else None,
                "receipt": {"audit_hash": "fixture" if self.audited else None},
                "output": {"values": {"delivery": deepcopy(self.payload)}}}


class FakeDestination:
    def __init__(self, lookup=False, replay=False):
        self.cap = Capabilities("owned-fixture", "synthetic-actor", lookup, replay)
        self.saved = {}
        self.calls = []
        self.lose_response = False
        self.on_send = None
    def capabilities(self):
        return self.cap
    def validate(self, payload):
        if payload.get("visibility") != "private":
            raise InvalidDelivery("private fixture only")
    def send(self, payload, operation_key):
        self.calls.append(operation_key)
        if operation_key not in self.saved:
            self.saved[operation_key] = Receipt("post-" + str(len(self.saved) + 1), fingerprint(payload))
        if self.on_send:
            self.on_send()
        if self.lose_response:
            raise TimeoutError("saved but reply lost")
        return self.saved[operation_key]
    def find(self, operation_key, payload_hash):
        return self.saved.get(operation_key)


class DeliveryTest(unittest.TestCase):
    def build(self, lookup=False, replay=False):
        self.store, self.results = MemoryDelivery(), AcceptedFixture()
        self.destination = FakeDestination(lookup, replay)
        self.outbox = Outbox(self.store, self.results, self.destination)
        self.reference = {"run_id": "owned-run", "node_id": "accepted-node"}

    def test_unknown_recovery_follows_three_actual_capabilities(self):
        for lookup, replay in [(True, False), (False, True), (False, False)]:
            with self.subTest(lookup=lookup, replay=replay):
                self.build(lookup, replay)
                self.destination.lose_response = True
                first = self.outbox.submit("business-operation", self.reference)
                self.assertEqual(first["state"], "unknown")
                self.assertEqual(self.outbox.submit("business-operation", self.reference)["state"], "unknown")
                self.assertEqual(len(self.destination.calls), 1)
                self.destination.lose_response = False
                recovered = self.outbox.reconcile(first["id"])
                self.assertEqual(recovered["state"], "confirmed" if lookup or replay else "unknown")
                self.assertEqual(len(self.destination.calls), 2 if replay else 1)
                self.assertEqual(len(self.destination.saved), 1)

    def test_same_business_key_different_payload_is_rejected(self):
        self.build()
        self.outbox.submit("business-operation", self.reference)
        self.results.payload["body"] = "different content"
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("business-operation", self.reference)
        self.assertEqual(len(self.destination.calls), 1)

    def test_new_run_and_whitespace_changes_do_not_duplicate_content(self):
        self.build()
        first = self.outbox.submit("business-operation", self.reference)
        self.results.payload["body"] = " synthetic   post "
        duplicate = self.outbox.submit("another-operation", {**self.reference, "run_id": "another-run"})
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["deduplicated_by"], "normalized_content")
        self.assertEqual(len(self.destination.calls), 1)

    def test_changed_environment_or_acceptance_blocks_recovery(self):
        self.build(replay=True)
        self.destination.lose_response = True
        first = self.outbox.submit("business-operation", self.reference)
        self.destination.cap = replace(self.destination.cap, environment_id="another-fixture")
        with self.assertRaises(InvalidDelivery):
            self.outbox.reconcile(first["id"])
        self.destination.cap = replace(self.destination.cap, environment_id="owned-fixture")
        self.results.payload["body"] = "changed"
        self.assertEqual(self.outbox.reconcile(first["id"])["state"], "unknown")
        self.assertEqual(len(self.destination.calls), 1)

    def test_stop_racing_external_completion_records_real_effect_without_resend(self):
        self.build(replay=True)
        self.destination.on_send = self.outbox.stop
        completed = self.outbox.submit("business-operation", self.reference)
        self.assertEqual(completed["state"], "confirmed")
        self.assertTrue(completed["completed_after_stop"])
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("new-operation", self.reference)
        self.assertEqual(len(self.destination.calls), 1)

    def test_effect_and_replay_budgets_are_not_model_turn_budgets(self):
        self.build(replay=True)
        self.destination.cap = replace(self.destination.cap, max_effects=2)
        self.destination.lose_response = True
        first = self.outbox.submit("business-operation", self.reference)
        for _ in range(4):
            self.outbox.reconcile(first["id"])
        self.assertEqual(len(self.destination.calls), 2)
        self.assertEqual(len(self.destination.saved), 1)
        self.results.payload["body"] = "new post"
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("new-operation", self.reference)

    def test_unaudited_results_have_zero_effects(self):
        self.build()
        self.results.audited = False
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("business-operation", self.reference)
        self.assertEqual(self.destination.calls, [])

    def test_persisted_payload_tampering_is_rejected_before_replay(self):
        self.build(replay=True)
        self.destination.lose_response = True
        operation = self.outbox.submit("business-operation", self.reference)
        self.store.ledger["operations"][operation["id"]]["payload"]["body"] = "unapproved change"
        with self.assertRaises(InvalidDelivery):
            self.outbox.reconcile(operation["id"])
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("business-operation", self.reference)
        self.assertEqual(len(self.destination.calls), 1)
