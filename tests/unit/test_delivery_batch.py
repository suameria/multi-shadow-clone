from copy import deepcopy
from dataclasses import replace
import unittest

from kagebunshin.delivery.application.outbox import Outbox
from kagebunshin.delivery.domain.model import InvalidDelivery
from tests.unit.test_delivery import AcceptedFixture, FakeDestination, MemoryDelivery


class BatchAccepted(AcceptedFixture):
    def __init__(self):
        super().__init__()
        self.payloads = {key: {"body": "synthetic " + key, "visibility": "private"} for key in ("a", "b", "c")}
        self.calls = 0
    def accepted_result(self, run_id, node_id):
        self.calls += 1
        result = super().accepted_result(run_id, node_id)
        result["output"]["values"]["delivery"] = deepcopy(self.payloads[node_id])
        return result


def request(key, node):
    return {"business_key": key, "reference": {"run_id": "fixture", "node_id": node}}


class DeliveryBatchTest(unittest.TestCase):
    def setUp(self):
        self.store, self.results, self.destination = MemoryDelivery(), BatchAccepted(), FakeDestination()
        self.outbox = Outbox(self.store, self.results, self.destination)

    def test_nine_hundred_requests_reserve_and_send_nothing(self):
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit_batch([request(str(i), "a") for i in range(900)])
        self.assertEqual(self.results.calls, 0)
        self.assertEqual(self.store.read()["revision"], 0)
        self.assertEqual(self.store.read()["operations"], {})
        self.assertEqual(self.destination.calls, [])

    def test_batch_reserves_all_unique_content_and_keeps_alias_binding(self):
        rows = self.outbox.submit_batch([request("one", "a"), request("two", "b"), request("alias", "a")])
        self.assertEqual([r["state"] for r in rows], ["confirmed"] * 3)
        self.assertEqual(rows[0]["id"], rows[2]["id"])
        self.assertEqual(rows[2]["deduplicated_by"], "normalized_content")
        self.assertEqual(len(self.destination.calls), 2)
        self.assertEqual(len(self.store.read()["bindings"]), 3)
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("alias", request("alias", "c")["reference"])
        self.assertEqual(len(self.destination.calls), 2)

    def test_insufficient_remaining_budget_rejects_the_whole_new_list(self):
        self.destination.cap = replace(self.destination.cap, max_effects=2)
        self.outbox.submit("one", request("one", "a")["reference"])
        before = self.store.read()
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit_batch([request("two", "b"), request("three", "c")])
        self.assertEqual(self.store.read(), before)
        self.assertEqual(len(self.destination.calls), 1)

    def test_invalid_last_payload_or_conflicting_key_has_no_prefix_effects(self):
        for invalid in (True, False):
            with self.subTest(invalid_payload=invalid):
                self.setUp()
                items = [request("one", "a"), request("two" if invalid else "one", "b")]
                if invalid:
                    self.results.payloads["b"]["visibility"] = "public"
                with self.assertRaises(InvalidDelivery):
                    self.outbox.submit_batch(items)
                self.assertEqual(self.store.read()["revision"], 0)
                self.assertEqual(self.destination.calls, [])

    def test_stop_cancels_only_unsent_reservations_and_requires_a_new_key(self):
        self.destination.cap = replace(self.destination.cap, max_effects=2)
        self.destination.on_send = self.outbox.stop
        rows = self.outbox.submit_batch([request("one", "a"), request("two", "b")])
        self.assertEqual([r["state"] for r in rows], ["confirmed", "cancelled"])
        self.assertEqual([r["submissions"] for r in rows], [1, 0])
        self.assertTrue(rows[0]["completed_after_stop"])
        self.destination.on_send = None
        self.outbox.resume()
        self.assertEqual(self.outbox.reconcile(rows[1]["id"])["state"], "cancelled")
        self.assertEqual(self.outbox.submit("two", request("two", "b")["reference"])["state"], "cancelled")
        self.assertEqual(len(self.destination.calls), 1)
        restarted = self.outbox.submit("explicit-new-request", request("new", "b")["reference"])
        self.assertEqual(restarted["state"], "confirmed")
        self.assertNotEqual(restarted["id"], rows[1]["id"])
        self.assertEqual(len(self.destination.calls), 2)
        self.assertEqual(self.outbox.submit("two", request("two", "b")["reference"])["state"], "cancelled")
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("two", request("two", "c")["reference"])

    def test_repeated_control_does_not_invalidate_current_reservations(self):
        self.destination.lose_response = True
        rows = self.outbox.submit_batch([request("one", "a"), request("two", "b")])
        before = self.outbox.status()
        self.assertEqual(self.outbox.resume(), before)
        self.destination.lose_response = False
        self.assertEqual(self.outbox.reconcile(rows[1]["id"])["state"], "confirmed")
        stopped = self.outbox.stop()
        self.assertEqual(self.outbox.stop(), stopped)

    def test_stop_does_not_refund_an_unknown_attempt(self):
        self.destination.cap = replace(self.destination.cap, max_effects=1)
        self.destination.lose_response = True
        row = self.outbox.submit("one", request("one", "a")["reference"])
        self.outbox.stop()
        self.outbox.resume()
        self.assertEqual(self.outbox.reconcile(row["id"])["state"], "unknown")
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("two", request("two", "b")["reference"])
        self.assertEqual(len(self.destination.calls), 1)

    def test_unsent_reserved_operation_can_start_after_an_uncertain_sibling(self):
        self.destination.lose_response = True
        rows = self.outbox.submit_batch([request("one", "a"), request("two", "b")])
        self.assertEqual([r["state"] for r in rows], ["unknown", "reserved"])
        self.assertEqual(len(self.destination.calls), 1)
        self.destination.lose_response = False
        # This destination provides neither lookup nor idempotent replay. A
        # reserved row is provably unsent; an unknown row is not resent.
        self.assertEqual(self.outbox.reconcile(rows[0]["id"])["state"], "unknown")
        self.assertEqual(self.outbox.reconcile(rows[1]["id"])["state"], "confirmed")
        self.assertEqual(len(self.destination.calls), 2)

    def test_source_change_in_last_item_before_first_send_preserves_zero_effects(self):
        base_save = self.store.save
        changed = False
        def save(ledger, revision):
            nonlocal changed
            base_save(ledger, revision)
            if not changed:
                self.results.payloads["b"]["body"] = "changed after admission"
                changed = True
        self.store.save = save
        rows = self.outbox.submit_batch([request("one", "a"), request("two", "b")])
        self.assertEqual([r["state"] for r in rows], ["reserved", "reserved"])
        self.assertEqual(self.destination.calls, [])

    def test_tampered_attempt_count_cannot_create_more_budget(self):
        row = self.outbox.submit("one", request("one", "a")["reference"])
        self.store.ledger["operations"][row["id"]]["submissions"] = -100
        with self.assertRaises(InvalidDelivery):
            self.outbox.submit("two", request("two", "b")["reference"])
        self.assertEqual(len(self.destination.calls), 1)
