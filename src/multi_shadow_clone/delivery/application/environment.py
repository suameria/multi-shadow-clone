"""Owned environment phases. Repository commands and files are injected ports."""

from copy import deepcopy
from uuid import uuid4

from ..domain.model import InvalidDelivery
from ..ports import EnvironmentRepository, LifecycleStore


class EnvironmentLifecycle:
    def __init__(self, store: LifecycleStore, repository: EnvironmentRepository, identifier=lambda: uuid4().hex):
        self.store, self.repository, self.identifier = store, repository, identifier

    def locked(self):
        return self.store.locked()

    def state(self):
        return self.store.read()

    def _phase(self, state, phase, **changes):
        state.update(phase=phase, **changes)
        self.store.save(state)

    def _execute(self, state, operation, phase):
        self._phase(state, phase, operation=operation, operation_id=self.identifier())
        return self.repository.execute(state, operation, state["operation_id"])

    def endpoint(self, expected=None):
        state = self.state()
        if not state or state["phase"] != "running" or (expected is not None and expected != state):
            raise InvalidDelivery("owned service is not current and running")
        return self.repository.endpoint(state)

    def start(self):
        with self.locked():
            state = self.state()
            if state and state["phase"] == "running":
                self.endpoint(state)
                return state
            if state and state["phase"] not in {"created", "started", "seeded", "retired"}:
                raise InvalidDelivery("lifecycle has an unfinished operation; use recover, never repeat creation or seed")
            if not state or state["phase"] == "retired":
                # Canonical Suameria tasks forbid components beginning with digits.
                suffix = self.identifier()[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
                state = self.repository.new_identity("multi-shadow-clone-local-" + suffix, "environment-" + self.identifier())
                self._execute(state, "create", "creating")
                self.repository.creation_ready(state)
                self._phase(state, "created")
            self.repository.validate(state)
            if state["phase"] == "created":
                self._execute(state, "start", "starting")
                self._phase(state, "started")
            if not state["seeded"]:
                self._execute(state, "seed", "seeding")
                # Persist the successful seed before reading service identity.
                self._phase(state, "seeded", seeded=True)
            self._phase(state, "running", **self.repository.describe(state))
            self.endpoint(state)
            return state

    def recover(self):
        """Reconcile exact evidence; ambiguous seed never becomes another seed."""
        with self.locked():
            state = self.state()
            if not state or state["phase"] in {"retired", "created", "started", "seeded", "service_stopped"}:
                return state or {"phase": "not_created"}
            phase = state["phase"]
            if phase == "running":
                self.endpoint(state)
                return state
            if phase == "creating":
                rejected = self.repository.creation_rejection(state)
                if rejected is not None:
                    self._phase(state, "retired", terminal=rejected, recovered=True)
                    return state
                # Official immutable identity + registration + creation HEAD
                # can prove creation even if our parent died before its receipt.
                self.repository.creation_ready(state)
                self._phase(state, "created", recovered=True)
                return state
            if phase in {"starting", "seeding", "stopping"}:
                result = self.repository.receipt(state)
                if result is None or result["returncode"] != 0:
                    raise InvalidDelivery("operation outcome remains unknown; keep evidence and retire the exact disposable environment")
                self.repository.validate(state)
                next_phase = {"starting": "started", "seeding": "seeded", "stopping": "service_stopped"}[phase]
                self._phase(state, next_phase, seeded=True if phase == "seeding" else state["seeded"], recovered=True)
                return state
            if phase == "cleaning":
                # Official retirement receipts survive deletion of the worktree.
                proof = self.repository.retirement_proof(state)
                if proof is not None:
                    self._phase(state, "retired", terminal=proof, recovered=True)
                    return state
                self.repository.no_change_retirement(state)
                # The official resume path performs cleanup only, no commits or push.
                self._execute(state, "resume_cleanup", "cleaning")
                proof = self.repository.retirement_proof(state)
                if proof is None:
                    raise InvalidDelivery("official cleanup did not provide retirement proof")
                self._phase(state, "retired", terminal=proof, recovered=True)
                return state
            raise InvalidDelivery("unsupported lifecycle phase")

    def retire(self):
        with self.locked():
            state = self.state()
            if not state or state["phase"] == "retired":
                return state or {"phase": "not_created"}
            if state["phase"] == "cleaning":
                raise InvalidDelivery("use recover to reconcile the exact cleanup; do not repeat finalization")
            # Includes unknown seed: disposal is allowed, repeating seed is not.
            self.repository.no_change_retirement(state)
            if state["phase"] != "service_stopped":
                self._execute(state, "stop", "stopping")
                self._phase(state, "service_stopped")
            self._execute(state, "retire", "cleaning")
            proof = self.repository.retirement_proof(state)
            if proof is None:
                raise InvalidDelivery("official retirement proof is missing; keep exact lifecycle")
            self._phase(state, "retired", terminal=proof)
            return deepcopy(state)
