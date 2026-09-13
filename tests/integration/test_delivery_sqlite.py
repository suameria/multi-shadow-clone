from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from multi_shadow_clone.delivery.application.outbox import Outbox
from multi_shadow_clone.delivery.infrastructure.sqlite_store import SQLiteDeliveryStore
from multi_shadow_clone.delivery.ports import Conflict
from multi_shadow_clone.delivery.domain.model import InvalidDelivery
from tests.unit.test_delivery_batch import BatchAccepted, request
from tests.unit.test_delivery import AcceptedFixture, FakeDestination


class DeliverySQLiteTest(unittest.TestCase):
    def test_stop_competes_with_a_durable_send_claim_without_refunding_unknown(self):
        for stage in ("reserved", "unknown"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "outbox.sqlite3"
                paused, release = threading.Event(), threading.Event()
                class PausingStore(SQLiteDeliveryStore):
                    armed = True
                    def save(self, ledger, revision):
                        super().save(ledger, revision)
                        if self.armed and any(op["state"] == stage for op in ledger["operations"].values()):
                            self.armed = False
                            paused.set()
                            if not release.wait(timeout=5):
                                raise TimeoutError("test did not release the committed reservation")
                destination = FakeDestination()
                destination.cap = replace(destination.cap, max_effects=1)
                results = BatchAccepted()
                dispatch = Outbox(PausingStore(path), results, destination)
                controller = Outbox(SQLiteDeliveryStore(path), results, destination)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(dispatch.submit, "one", request("one", "a")["reference"])
                    try:
                        self.assertTrue(paused.wait(timeout=5))
                        stopped = controller.stop()
                        expected = "cancelled" if stage == "reserved" else "unknown"
                        self.assertEqual([op["state"] for op in stopped["operations"].values()], [expected])
                    finally:
                        release.set()
                    row = pending.result(timeout=5)
                self.assertEqual(row["state"], expected)
                self.assertEqual(destination.calls, [])
                reopened = Outbox(SQLiteDeliveryStore(path), results, destination)
                reopened.resume()
                self.assertEqual(reopened.reconcile(row["id"])["state"], expected)
                self.assertEqual(destination.calls, [])
                if stage == "reserved":
                    self.assertEqual(reopened.submit("new", request("new", "a")["reference"])["state"], "confirmed")
                    self.assertEqual(len(destination.calls), 1)
                else:
                    with self.assertRaises(InvalidDelivery):
                        reopened.submit("new", request("new", "b")["reference"])
                    self.assertEqual(destination.calls, [])

    def test_schema_one_migration_preserves_receipts_and_adds_primary_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.sqlite3"
            outbox = Outbox(SQLiteDeliveryStore(path), BatchAccepted(), FakeDestination())
            original = outbox.submit("one", request("one", "a")["reference"])
            legacy = outbox.status()
            legacy.pop("bindings")
            with sqlite3.connect(path) as db:
                db.execute("UPDATE outbox SET snapshot=? WHERE id=1", (json.dumps(legacy),))
                db.execute("PRAGMA user_version=1")
            migrated = SQLiteDeliveryStore(path)
            ledger = migrated.read()
            self.assertEqual(ledger["operations"][original["id"]], original)
            self.assertEqual(ledger["bindings"][original["id"]], {"binding": original["binding"], "target": original["id"]})
            self.assertEqual(ledger["revision"], legacy["revision"] + 1)
            with self.assertRaises(Conflict):
                migrated.save(legacy, legacy["revision"])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_deduplicated_alias_survives_reopen_and_rejects_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.sqlite3"
            results, destination = BatchAccepted(), FakeDestination()
            outbox = Outbox(SQLiteDeliveryStore(path), results, destination)
            original = outbox.submit("one", request("one", "a")["reference"])
            alias = outbox.submit("alias", request("alias", "a")["reference"])
            self.assertEqual(original["id"], alias["id"])
            reopened = Outbox(SQLiteDeliveryStore(path), results, destination)
            with self.assertRaises(InvalidDelivery):
                reopened.submit("alias", request("alias", "b")["reference"])
            self.assertEqual(len(destination.calls), 1)

    def test_competing_batches_reserve_all_or_none_against_shared_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.sqlite3"
            barrier = threading.Barrier(2)
            class GatedStore(SQLiteDeliveryStore):
                first = True
                def read(self):
                    snapshot = super().read()
                    if self.first:
                        self.first = False
                        barrier.wait(timeout=3)
                    return snapshot
            destination = FakeDestination()
            destination.cap = replace(destination.cap, max_effects=3)
            a, b = BatchAccepted(), BatchAccepted()
            b.payloads = {k: {"body": "other " + k, "visibility": "private"} for k in b.payloads}
            boxes = [Outbox(GatedStore(path), a, destination), Outbox(GatedStore(path), b, destination)]
            def run(index):
                try:
                    return boxes[index].submit_batch([request(str(index)+"a", "a"), request(str(index)+"b", "b")])
                except InvalidDelivery:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(run, [0, 1]))
            self.assertEqual(sum(x is not None for x in outcomes), 1)
            ledger = SQLiteDeliveryStore(path).read()
            self.assertEqual(len(ledger["operations"]), 2)
            self.assertEqual(len(ledger["bindings"]), 2)
            self.assertEqual(sum(p["submissions"] for p in ledger["operations"].values()), 2)
            self.assertEqual(len(destination.calls), 2)

    def test_durable_unknown_prevents_duplicate_after_process_adapter_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.sqlite3"
            store = SQLiteDeliveryStore(path)
            results, destination = AcceptedFixture(), FakeDestination(lookup=True)
            destination.lose_response = True
            outbox = Outbox(store, results, destination)
            reference = {"run_id": "fixture", "node_id": "fixture"}
            first = outbox.submit("same-business-operation", reference)
            stale = store.read()
            restarted = Outbox(SQLiteDeliveryStore(path), results, destination)
            self.assertEqual(restarted.submit("same-business-operation", reference)["state"], "unknown")
            self.assertEqual(len(destination.calls), 1)
            self.assertEqual(restarted.reconcile(first["id"])["state"], "confirmed")
            with self.assertRaises(Conflict):
                store.save(stale, stale["revision"])
            self.assertEqual(len(destination.saved), 1)
