"""Actual owned process termination and SQLite failures; no model/network calls."""

from contextlib import contextmanager
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.domain.contracts import Node, Plan
from kagebunshin.orchestration.infrastructure.sqlite_store import SQLiteRunStore
from tests.unit.fakes import ROLES, ScriptedProvider


CHILD = r'''
import signal, sys
from pathlib import Path
sys.path[:0] = [str(Path(sys.argv[1]) / "src"), sys.argv[1]]
from kagebunshin.orchestration.application.engine import Engine
from kagebunshin.orchestration.infrastructure.sqlite_store import SQLiteRunStore
from tests.unit.fakes import ROLES, ScriptedProvider

path, run_id, phase = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
def checkpoint():
    print("CHECKPOINT", flush=True)
    signal.pause()

if phase == "transaction":
    class InterruptedStore(SQLiteRunStore):
        @staticmethod
        def _append_events(db, record, after):
            SQLiteRunStore._append_events(db, record, after)
            checkpoint()
    store = InterruptedStore(path)
    run = store.read(run_id)
    run["state"] = "completed"
    run["events"].append({"seq": len(run["events"])+1, "kind": "uncommitted"})
    store.save(run, run["revision"])
elif phase == "candidate-saved":
    Engine(SQLiteRunStore(path), ScriptedProvider(), ROLES, lambda: 1000).step(run_id)
    checkpoint()
else:
    def execute(request):
        if phase != "dispatch-intent":
            request.progress("owned-fixture-thread", None)
        if phase == "turn-handle":
            request.progress("owned-fixture-thread", "owned-fixture-turn")
        checkpoint()
    Engine(SQLiteRunStore(path), ScriptedProvider(execute), ROLES, lambda: 1000).step(run_id)
'''


class ProcessRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        self.store = SQLiteRunStore(self.path)
        self.engine = Engine(self.store, ScriptedProvider(), ROLES, lambda: 1000)

    def kill_at(self, run_id, phase):
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", CHILD, str(Path(__file__).resolve().parents[2]),
             str(self.path), run_id, phase], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(ready.select(timeout=10), "child did not reach checkpoint")
                self.assertEqual(process.stdout.readline(), b"CHECKPOINT\n")
            process.send_signal(signal.SIGKILL)
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL, stderr.decode())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_sigkill_before_and_after_handles_keeps_reservation_without_resending(self):
        for phase in ("dispatch-intent", "thread-handle", "turn-handle"):
            with self.subTest(phase=phase):
                # Separate stores keep the independent crash fixtures below the global two-slot limit.
                self.path = Path(self.temp.name) / (phase + ".sqlite3")
                store = SQLiteRunStore(self.path)
                engine = Engine(store, ScriptedProvider(), ROLES, lambda: 1000)
                run_id = engine.create(Plan("synthetic crash", (Node("a", "R07", "a"),), {}))
                self.kill_at(run_id, phase)
                provider = ScriptedProvider()
                recovered = Engine(SQLiteRunStore(self.path), provider, ROLES, lambda: 1000)
                for _ in range(2):
                    recovered.reconcile(run_id)
                    recovered.run_until_idle(run_id)
                saved = recovered.status(run_id)
                self.assertEqual(saved["turns"], 1)
                self.assertEqual(len(saved["attempts"]), 1)
                self.assertIsNotNone(saved["nodes"]["a"]["active"])
                self.assertIsNone(saved["nodes"]["a"]["output"])
                self.assertEqual(provider.requests, [])
                expected = None if phase == "dispatch-intent" else "owned-fixture-thread"
                self.assertEqual(saved["attempts"][0]["thread_id"], expected)
                self.assertEqual(saved["attempts"][0]["turn_id"], "owned-fixture-turn" if phase == "turn-handle" else None)

    def test_sigkill_after_saved_candidate_resumes_at_audit_without_regeneration(self):
        run_id = self.engine.create(Plan("synthetic crash", (Node("a", "R07", "a", audit_role="R12"),), {}))
        self.kill_at(run_id, "candidate-saved")
        self.assertEqual(self.store.read(run_id)["nodes"]["a"]["state"], "audit_pending")
        provider = ScriptedProvider()
        recovered = Engine(SQLiteRunStore(self.path), provider, ROLES, lambda: 1000)
        result = recovered.run_until_idle(run_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turns"], 2)
        self.assertEqual([r.stage for r in provider.requests], ["audit"])

    def test_sigkill_during_sqlite_transaction_commits_neither_event_nor_snapshot(self):
        run_id = self.engine.create(Plan("synthetic crash", (Node("a", "R07", "a"),), {}))
        before = self.store.read(run_id)
        self.kill_at(run_id, "transaction")
        self.assertEqual(SQLiteRunStore(self.path).read(run_id), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], len(before["events"]))

    def test_sqlite_full_rolls_back_preceding_event_and_snapshot(self):
        run_id = self.engine.create(Plan("synthetic disk limit", (Node("a", "R07", "a"),), {}))
        before = self.store.read(run_id)
        class LimitedStore(SQLiteRunStore):
            @contextmanager
            def _connection(self):
                with super()._connection() as db:
                    pages = db.execute("PRAGMA page_count").fetchone()[0]
                    db.execute("PRAGMA max_page_count=" + str(pages))
                    yield db
        limited = LimitedStore(self.path)
        changed = limited.read(run_id)
        changed["state"] = "completed"
        changed["events"].extend([
            {"seq": len(before["events"]) + 1, "kind": "first-small-event"},
            {"seq": len(before["events"]) + 2, "kind": "large-event", "fixture": "x" * 500_000},
        ])
        with self.assertRaises(sqlite3.OperationalError) as error:
            limited.save(changed, changed["revision"])
        self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(self.store.read(run_id), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], len(before["events"]))
