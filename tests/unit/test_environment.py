from contextlib import nullcontext
from copy import deepcopy
import unittest

from multi_shadow_clone.delivery.application.environment import EnvironmentLifecycle
from multi_shadow_clone.delivery.domain.model import InvalidDelivery


class MemoryLifecycle:
    def __init__(self):
        self.value, self.fail_phase = None, None

    def locked(self): return nullcontext()
    def read(self): return deepcopy(self.value)
    def save(self, value):
        if value["phase"] == self.fail_phase:
            self.fail_phase = None
            raise InterruptedError("lost after command before phase commit")
        self.value = deepcopy(value)


class OwnedRepository:
    def __init__(self):
        self.calls, self.receipts = [], {}
        self.created = False
        self.retired = False
        self.fail = None
        self.fail_describe = False
        self.changed = False
        self.retirement_proven = True

    def new_identity(self, task, environment_id):
        return {"id": environment_id, "task": task, "path": "owned-path", "branch": "owned-branch", "head": "h", "seeded": False}
    def validate(self, state):
        if self.changed or not self.created: raise InvalidDelivery("identity changed")
    def creation_ready(self, state): self.validate(state)
    def creation_rejection(self, state): return None
    def describe(self, state):
        if self.fail_describe: raise InvalidDelivery("port not available")
        return {"port": 12345}
    def endpoint(self, state): return "http://127.0.0.1:12345"
    def no_change_retirement(self, state): self.validate(state)
    def retirement_proof(self, state):
        return {"status": "passed"} if self.retired and self.retirement_proven else None
    def receipt(self, state): return self.receipts.get(state["operation_id"])
    def execute(self, state, operation, operation_id):
        self.calls.append(operation)
        if operation == "create": self.created = True
        if operation in {"retire", "resume_cleanup"}: self.retired = True
        if self.fail == operation: raise InterruptedError("unknown response")
        self.receipts[operation_id] = {"returncode": 0}
        return "ok"


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.store, self.repo = MemoryLifecycle(), OwnedRepository()
        self.environment = EnvironmentLifecycle(self.store, self.repo)

    def test_generated_task_never_begins_a_component_with_digits(self):
        env = EnvironmentLifecycle(self.store, self.repo, identifier=lambda: "01234567" + "a" * 24)
        state = env.start()
        self.assertEqual(state["task"], "multi-shadow-clone-local-ghijklmn")
        self.assertFalse(any(part[0].isdigit() for part in state["task"].split("-")))

    def test_proven_precreation_rejection_retires_only_empty_reservation(self):
        self.store.value = {"id": "owned", "phase": "creating"}
        proof = {"status": "rejected_before_creation", "resources_created": False}
        self.repo.creation_rejection = lambda state: proof
        recovered = self.environment.recover()
        self.assertEqual(recovered["phase"], "retired")
        self.assertEqual(recovered["terminal"], proof)
        self.assertEqual(self.repo.calls, [])

    def test_creation_reply_loss_uses_identity_without_another_creation(self):
        self.repo.fail = "create"
        with self.assertRaises(InterruptedError): self.environment.start()
        with self.assertRaises(InvalidDelivery): self.environment.start()
        self.assertEqual(self.environment.recover()["phase"], "created")
        self.repo.fail = None
        self.assertEqual(self.environment.start()["phase"], "running")
        self.assertEqual(self.repo.calls.count("create"), 1)

    def test_seed_success_is_saved_before_port_observation_and_not_repeated(self):
        self.repo.fail_describe = True
        with self.assertRaises(InvalidDelivery): self.environment.start()
        self.assertEqual(self.store.read()["phase"], "seeded")
        self.assertTrue(self.store.read()["seeded"])
        self.repo.fail_describe = False
        self.environment.start()
        self.assertEqual(self.repo.calls.count("seed"), 1)

    def test_phase_commit_crash_recovers_successful_start_seed_and_stop_receipts(self):
        for after in ("started", "seeded", "service_stopped"):
            with self.subTest(after=after):
                self.setUp()
                if after == "service_stopped": self.environment.start()
                self.store.fail_phase = after
                with self.assertRaises(InterruptedError):
                    self.environment.retire() if after == "service_stopped" else self.environment.start()
                calls = list(self.repo.calls)
                self.assertEqual(self.environment.recover()["phase"], after)
                self.assertEqual(calls, self.repo.calls)

    def test_unknown_seed_cannot_be_repeated_but_owned_environment_can_be_retired(self):
        self.repo.fail = "seed"
        with self.assertRaises(InterruptedError): self.environment.start()
        with self.assertRaises(InvalidDelivery): self.environment.recover()
        with self.assertRaises(InvalidDelivery): self.environment.start()
        self.repo.fail = None
        self.assertEqual(self.environment.retire()["phase"], "retired")
        self.assertEqual(self.repo.calls.count("seed"), 1)

    def test_cleanup_reply_loss_uses_durable_official_proof(self):
        self.environment.start()
        self.repo.fail = "retire"
        with self.assertRaises(InterruptedError): self.environment.retire()
        with self.assertRaises(InvalidDelivery): self.environment.retire()
        self.assertEqual(self.environment.recover()["phase"], "retired")
        self.assertEqual(self.repo.calls.count("retire"), 1)
        self.assertNotIn("resume_cleanup", self.repo.calls)

    def test_incomplete_cleanup_uses_only_exact_no_change_resume(self):
        self.environment.start()
        self.repo.fail = "retire"
        with self.assertRaises(InterruptedError): self.environment.retire()
        self.repo.retired = False
        self.repo.fail = None
        self.assertEqual(self.environment.recover()["phase"], "retired")
        self.assertEqual(self.repo.calls[-1], "resume_cleanup")

    def test_changed_ownership_prevents_cleanup_effects(self):
        self.environment.start()
        count = len(self.repo.calls)
        self.repo.changed = True
        with self.assertRaises(InvalidDelivery): self.environment.retire()
        self.assertEqual(len(self.repo.calls), count)
